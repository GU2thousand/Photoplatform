package com.generatecloud.app.websocket;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.generatecloud.app.dto.TeamEventResponse;
import com.generatecloud.app.entity.UserAccount;
import com.generatecloud.app.entity.enums.Role;
import com.generatecloud.app.repository.TeamMemberRepository;
import com.generatecloud.app.repository.UserAccountRepository;
import com.generatecloud.app.service.JwtService;
import java.net.URI;
import java.time.Instant;
import java.util.HashMap;
import java.util.List;
import java.util.Optional;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.TimeUnit;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;
import org.springframework.amqp.core.Binding;
import org.springframework.amqp.core.Declarables;
import org.springframework.amqp.core.FanoutExchange;
import org.springframework.amqp.core.Message;
import org.springframework.amqp.core.MessageListener;
import org.springframework.amqp.core.MessageProperties;
import org.springframework.amqp.core.Queue;
import org.springframework.amqp.rabbit.connection.CachingConnectionFactory;
import org.springframework.amqp.rabbit.core.RabbitAdmin;
import org.springframework.amqp.rabbit.core.RabbitTemplate;
import org.springframework.amqp.rabbit.listener.SimpleMessageListenerContainer;
import org.springframework.mock.env.MockEnvironment;
import org.springframework.web.socket.TextMessage;
import org.springframework.web.socket.WebSocketSession;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.doAnswer;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

/** Opt-in protocol test for CI's RabbitMQ 4.3 service; never silently substitutes a mocked broker. */
@EnabledIfEnvironmentVariable(named = "RABBITMQ_RELAY_TEST", matches = "true")
class TeamEventRelayIntegrationTests {
    private final ObjectMapper mapper = new ObjectMapper().findAndRegisterModules();
    private final JwtService jwt = new JwtService("test-secret-key-for-jwt-signing-1234567890", 86400000,
            new MockEnvironment());

    @Test
    void actualFanoutDeliversAcrossApiConnectionsWithoutLocalEchoOrDuplicate() throws Exception {
        UserAccountRepository users = mock(UserAccountRepository.class);
        TeamMemberRepository members = mock(TeamMemberRepository.class);
        UserAccount user = UserAccount.builder().id(1L).email("relay@example.com").name("Relay user")
                .role(Role.USER).build();
        when(users.findById(1L)).thenReturn(Optional.of(user));
        when(members.existsByTeamIdAndUserId(10L, 1L)).thenReturn(true);

        try (ApiInstance first = new ApiInstance(users, members, user);
                ApiInstance second = new ApiInstance(users, members, user)) {
            TeamEventResponse firstEvent = event("first API event");
            first.handler.broadcast(10L, firstEvent);
            Message original = first.awaitConsumed();
            second.awaitConsumed();
            assertThat(first.deliveries).containsExactly(firstEvent);
            assertThat(second.deliveries).containsExactly(firstEvent);

            TeamEventResponse secondEvent = event("second API event");
            second.handler.broadcast(10L, secondEvent);
            first.awaitConsumed();
            second.awaitConsumed();
            assertThat(first.deliveries).containsExactly(firstEvent, secondEvent);
            assertThat(second.deliveries).containsExactly(firstEvent, secondEvent);

            // Exercise a real duplicate broker delivery with the original event ID and origin.
            new RabbitTemplate(first.connection).send(RabbitTeamEventRelay.EXCHANGE, "",
                    new Message(original.getBody(), new MessageProperties()));
            first.awaitConsumed();
            second.awaitConsumed();
            assertThat(first.deliveries).containsExactly(firstEvent, secondEvent);
            assertThat(second.deliveries).containsExactly(firstEvent, secondEvent);
        }
    }

    private TeamEventResponse event(String text) {
        return new TeamEventResponse("NOTE", text, null, 10L, "Relay user", Instant.now());
    }

    private final class ApiInstance implements AutoCloseable {
        private final CachingConnectionFactory connection;
        private final RabbitTeamEventRelay relay;
        private final SimpleMessageListenerContainer listener;
        private final TeamCollaborationWebSocketHandler handler;
        private final LinkedBlockingQueue<Message> consumed = new LinkedBlockingQueue<>(10);
        private final List<TeamEventResponse> deliveries = new CopyOnWriteArrayList<>();

        ApiInstance(UserAccountRepository users, TeamMemberRepository members, UserAccount user) throws Exception {
            connection = new CachingConnectionFactory(environment("RABBITMQ_HOST", "localhost"),
                    Integer.parseInt(environment("RABBITMQ_PORT", "5672")));
            connection.setUsername(environment("RABBITMQ_USER", "generatecloud"));
            connection.setPassword(environment("RABBITMQ_PASSWORD", "generatecloud"));
            connection.setVirtualHost(environment("RABBITMQ_VHOST", "/"));
            connection.setConnectionTimeout(5000);
            connection.setPublisherConfirmType(CachingConnectionFactory.ConfirmType.CORRELATED);
            connection.setPublisherReturns(true);

            TeamEventRelayConfiguration configuration = new TeamEventRelayConfiguration();
            relay = configuration.teamEventRelay(mapper, connection);
            listener = configuration.teamLiveListener(connection, relay);
            handler = new TeamCollaborationWebSocketHandler(mapper, jwt, users, members);
            handler.setEventRelay(relay);
            try {
                // Declare exactly the production fanout/exclusive-classic topology on each API connection.
                Declarables topology = configuration.teamLiveTopology();
                RabbitAdmin admin = new RabbitAdmin(connection);
                for (FanoutExchange exchange : topology.getDeclarablesByType(FanoutExchange.class)) {
                    admin.declareExchange(exchange);
                }
                for (Queue queue : topology.getDeclarablesByType(Queue.class)) {
                    admin.declareQueue(queue);
                }
                for (Binding binding : topology.getDeclarablesByType(Binding.class)) {
                    admin.declareBinding(binding);
                }
                listener.setMessageListener((MessageListener) message -> {
                    relay.receive(message);
                    consumed.add(message);
                });
                listener.afterPropertiesSet();
                listener.start();
                connectSocket(user);
            } catch (Exception exception) {
                close();
                throw exception;
            }
        }

        private void connectSocket(UserAccount user) throws Exception {
            WebSocketSession session = mock(WebSocketSession.class);
            when(session.getAttributes()).thenReturn(new HashMap<>());
            when(session.getUri()).thenReturn(URI.create("ws://localhost/ws/teams/10?ticket="
                    + jwt.generateSocketTicket(user.getEmail(), user.getId(), 10L).ticket()));
            when(session.isOpen()).thenReturn(true);
            doAnswer(invocation -> {
                TextMessage message = invocation.getArgument(0);
                deliveries.add(mapper.readValue(message.getPayload(), TeamEventResponse.class));
                return null;
            }).when(session).sendMessage(any(TextMessage.class));
            handler.afterConnectionEstablished(session);
        }

        Message awaitConsumed() throws InterruptedException {
            Message message = consumed.poll(10, TimeUnit.SECONDS);
            assertThat(message).as("real RabbitMQ fanout delivery to each API task queue").isNotNull();
            return message;
        }

        @Override
        public void close() {
            listener.stop();
            relay.close();
            connection.destroy();
        }
    }

    private static String environment(String name, String fallback) {
        String value = System.getenv(name);
        return value == null || value.isBlank() ? fallback : value;
    }
}
