package com.generatecloud.app.websocket;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.generatecloud.app.dto.TeamEventResponse;
import com.generatecloud.app.entity.UserAccount;
import com.generatecloud.app.entity.enums.Role;
import com.generatecloud.app.repository.TeamMemberRepository;
import com.generatecloud.app.repository.UserAccountRepository;
import com.generatecloud.app.service.JwtService;
import java.net.URI;
import java.time.Duration;
import java.time.Instant;
import java.util.HashMap;
import java.util.Optional;
import java.util.UUID;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.springframework.amqp.core.Message;
import org.springframework.amqp.core.MessageProperties;
import org.springframework.amqp.core.Queue;
import org.springframework.amqp.rabbit.core.RabbitTemplate;
import org.springframework.boot.test.context.runner.ApplicationContextRunner;
import org.springframework.boot.test.system.CapturedOutput;
import org.springframework.boot.test.system.OutputCaptureExtension;
import org.springframework.mock.env.MockEnvironment;
import org.springframework.transaction.support.TransactionSynchronization;
import org.springframework.transaction.support.TransactionSynchronizationManager;
import org.springframework.transaction.support.TransactionSynchronizationUtils;
import org.springframework.web.socket.CloseStatus;
import org.springframework.web.socket.TextMessage;
import org.springframework.web.socket.WebSocketSession;

import static org.assertj.core.api.Assertions.assertThat;
import static org.junit.jupiter.api.Assertions.assertTimeoutPreemptively;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.doAnswer;
import static org.mockito.Mockito.doThrow;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

@ExtendWith(OutputCaptureExtension.class)
class TeamEventRelayTests {
    private final ObjectMapper mapper = new ObjectMapper().findAndRegisterModules();
    private final JwtService jwt = new JwtService("test-secret-key-for-jwt-signing-1234567890", 86400000,
            new MockEnvironment());
    private UserAccountRepository users;
    private TeamMemberRepository members;
    private TeamCollaborationWebSocketHandler first;
    private TeamCollaborationWebSocketHandler second;
    private RabbitTemplate broker;
    private RabbitTeamEventRelay firstRelay;
    private RabbitTeamEventRelay secondRelay;
    private UserAccount user;

    @BeforeEach
    void setup() {
        users = mock(UserAccountRepository.class);
        members = mock(TeamMemberRepository.class);
        user = UserAccount.builder().id(1L).email("member@example.com").name("Member").role(Role.USER).build();
        when(users.findById(1L)).thenReturn(Optional.of(user));
        when(members.existsByTeamIdAndUserId(10L, 1L)).thenReturn(true);
        first = new TeamCollaborationWebSocketHandler(mapper, jwt, users, members);
        second = new TeamCollaborationWebSocketHandler(mapper, jwt, users, members);
        broker = mock(RabbitTemplate.class);
        firstRelay = new RabbitTeamEventRelay(mapper, broker, immediateExecutor(), "api-one");
        secondRelay = new RabbitTeamEventRelay(mapper, broker, immediateExecutor(), "api-two");
        first.setEventRelay(firstRelay);
        second.setEventRelay(secondRelay);
        doAnswer(invocation -> {
            Message message = invocation.getArgument(2);
            firstRelay.receive(message);
            secondRelay.receive(message);
            // A redelivery must not echo twice to any socket.
            secondRelay.receive(message);
            return null;
        }).when(broker).send(eq(RabbitTeamEventRelay.EXCHANGE), eq(""), any(Message.class));
    }

    @AfterEach
    void cleanup() {
        if (TransactionSynchronizationManager.isSynchronizationActive()) {
            TransactionSynchronizationManager.clearSynchronization();
        }
        TransactionSynchronizationManager.setActualTransactionActive(false);
        firstRelay.close();
        secondRelay.close();
    }

    @Test
    void twoApiInstancesDeliverOnceAndRemoteDeliveryDoesNotPublishAgain() throws Exception {
        WebSocketSession local = connect(first);
        WebSocketSession remote = connect(second);
        first.broadcast(10L, event());
        verify(local, times(1)).sendMessage(any(TextMessage.class));
        verify(remote, times(1)).sendMessage(any(TextMessage.class));
        verify(broker, times(1)).send(eq(RabbitTeamEventRelay.EXCHANGE), eq(""), any(Message.class));
    }

    @Test
    void uploadEventsRelayEvenWhenPublisherHasNoLocalSessions() throws Exception {
        WebSocketSession remote = connect(second);
        first.broadcast(10L, new TeamEventResponse("UPLOAD", "image created", 31L, 10L, "Member", Instant.now()));
        verify(remote).sendMessage(any(TextMessage.class));
        assertThat(first.activeConnections()).isZero();
    }

    @Test
    void remoteDeliveryRechecksRevokedMembershipAndDeletedAccount() throws Exception {
        WebSocketSession revoked = connect(second);
        when(members.existsByTeamIdAndUserId(10L, 1L)).thenReturn(false);
        first.broadcast(10L, event());
        verify(revoked).close(CloseStatus.POLICY_VIOLATION);
        verify(revoked, never()).sendMessage(any());
        assertThat(second.activeConnections()).isZero();

        when(members.existsByTeamIdAndUserId(10L, 1L)).thenReturn(true);
        WebSocketSession deleted = connect(second);
        when(users.findById(1L)).thenReturn(Optional.empty());
        first.broadcast(10L, event());
        verify(deleted).close(CloseStatus.POLICY_VIOLATION);
        verify(deleted, never()).sendMessage(any());
    }

    @Test
    void transactionEventsOnlyReachBothInstancesAfterCommit() throws Exception {
        WebSocketSession local = connect(first);
        WebSocketSession remote = connect(second);
        startTransaction();
        first.broadcast(10L, event());
        verify(local, never()).sendMessage(any());
        verify(remote, never()).sendMessage(any());
        verify(broker, never()).send(any(), any(), any(Message.class));

        TransactionSynchronizationUtils.triggerAfterCommit();
        verify(local).sendMessage(any(TextMessage.class));
        verify(remote).sendMessage(any(TextMessage.class));
    }

    @Test
    void rollbackDiscardsPendingNotification() throws Exception {
        WebSocketSession remote = connect(second);
        startTransaction();
        first.broadcast(10L, event());
        TransactionSynchronizationUtils.triggerAfterCompletion(TransactionSynchronization.STATUS_ROLLED_BACK);
        verify(remote, never()).sendMessage(any());
        verify(broker, never()).send(any(), any(), any(Message.class));
    }

    @Test
    void brokerFailureCannotEscapeAfterCommitAndLogsNoCredentials(CapturedOutput output) throws Exception {
        WebSocketSession local = connect(first);
        doThrow(new IllegalStateException("amqps://user:super-secret@broker.internal/private"))
                .when(broker).send(any(), any(), any(Message.class));
        startTransaction();
        first.broadcast(10L, event());
        first.broadcast(10L, event());
        TransactionSynchronizationUtils.triggerAfterCommit();
        verify(local, times(2)).sendMessage(any(TextMessage.class));
        assertThat(output.getOut()).contains("Team live notification dropped")
                .doesNotContain("super-secret", "broker.internal", "IllegalStateException");
        assertThat(output.getOut().split("Team live notification dropped", -1)).hasSize(2);
    }

    @Test
    void blockedBrokerCannotBlockRequestOrGrowPublicationBuffer() throws Exception {
        CountDownLatch started = new CountDownLatch(1);
        CountDownLatch unblock = new CountDownLatch(1);
        ThreadPoolExecutor executor = new ThreadPoolExecutor(1, 1, 0L, TimeUnit.MILLISECONDS,
                new ArrayBlockingQueue<>(2), new ThreadPoolExecutor.AbortPolicy());
        RabbitTemplate blocked = mock(RabbitTemplate.class);
        doAnswer(invocation -> {
            started.countDown();
            unblock.await(5, TimeUnit.SECONDS);
            return null;
        }).when(blocked).send(any(), any(), any(Message.class));
        try (RabbitTeamEventRelay relay = new RabbitTeamEventRelay(mapper, blocked, executor, "blocked-api")) {
            relay.publish(10L, event());
            assertThat(started.await(2, TimeUnit.SECONDS)).isTrue();
            assertTimeoutPreemptively(Duration.ofSeconds(1), () -> {
                for (int i = 0; i < 100; i++) {
                    relay.publish(10L, event());
                }
            });
            assertThat(executor.getQueue()).hasSize(2);
            assertThat(executor.getPoolSize()).isEqualTo(1);
        } finally {
            unblock.countDown();
        }
    }

    @Test
    void remoteMalformedOversizedStaleAndMismatchedTeamEventsAreDiscarded() throws Exception {
        WebSocketSession remote = connect(second);
        secondRelay.receive(new Message("not-json".getBytes(), new MessageProperties()));
        secondRelay.receive(new Message(new byte[RabbitTeamEventRelay.MAX_MESSAGE_BYTES + 1], new MessageProperties()));
        secondRelay.receive(envelope(11L, event(), System.currentTimeMillis()));
        secondRelay.receive(envelope(10L, event(), System.currentTimeMillis() - 60_000));
        verify(remote, never()).sendMessage(any());
    }

    @Test
    void ephemeralPerInstanceQueuesExplicitlyUseClassicAndBoundedRetention() {
        Queue queue = new TeamEventRelayConfiguration().teamLiveTopology().getDeclarablesByType(Queue.class).get(0);
        Queue other = new TeamEventRelayConfiguration().teamLiveTopology().getDeclarablesByType(Queue.class).get(0);
        assertThat(queue.getName()).isNotEqualTo(other.getName());
        assertThat(queue.isDurable()).isFalse();
        assertThat(queue.isExclusive()).isTrue();
        assertThat(queue.isAutoDelete()).isTrue();
        assertThat(queue.getArguments()).containsEntry("x-queue-type", "classic")
                .containsEntry("x-message-ttl", 30_000).containsEntry("x-expires", 120_000)
                .containsEntry("x-max-length", 1000).containsEntry("x-max-length-bytes", 4_194_304);
    }

    @Test
    void pipelineDisabledDoesNotCreateBrokerRelay() {
        new ApplicationContextRunner().withUserConfiguration(TeamEventRelayConfiguration.class)
                .run(context -> assertThat(context).doesNotHaveBean(TeamEventRelay.class));
    }

    private Message envelope(Long teamId, TeamEventResponse event, long createdAt) throws Exception {
        return new Message(mapper.writeValueAsBytes(new RabbitTeamEventRelay.Envelope(
                1, UUID.randomUUID(), "another-api", teamId, event, createdAt)), new MessageProperties());
    }

    private void startTransaction() {
        TransactionSynchronizationManager.setActualTransactionActive(true);
        TransactionSynchronizationManager.initSynchronization();
    }

    private ExecutorService immediateExecutor() {
        ExecutorService executor = mock(ExecutorService.class);
        doAnswer(invocation -> {
            invocation.<Runnable>getArgument(0).run();
            return null;
        }).when(executor).execute(any(Runnable.class));
        return executor;
    }

    private TeamEventResponse event() {
        return new TeamEventResponse("NOTE", "hello", null, 10L, "Member", Instant.now());
    }

    private WebSocketSession connect(TeamCollaborationWebSocketHandler handler) throws Exception {
        WebSocketSession session = mock(WebSocketSession.class);
        when(session.getAttributes()).thenReturn(new HashMap<>());
        when(session.getUri()).thenReturn(URI.create("ws://localhost/ws/teams/10?ticket="
                + jwt.generateSocketTicket(user.getEmail(), user.getId(), 10L).ticket()));
        when(session.isOpen()).thenReturn(true);
        handler.afterConnectionEstablished(session);
        return session;
    }
}
