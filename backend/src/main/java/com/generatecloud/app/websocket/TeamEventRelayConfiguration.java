package com.generatecloud.app.websocket;

import com.fasterxml.jackson.databind.ObjectMapper;
import java.util.Map;
import java.util.UUID;
import org.springframework.amqp.core.Binding;
import org.springframework.amqp.core.Declarables;
import org.springframework.amqp.core.FanoutExchange;
import org.springframework.amqp.core.Queue;
import org.springframework.amqp.rabbit.connection.ConnectionFactory;
import org.springframework.amqp.rabbit.core.RabbitTemplate;
import org.springframework.amqp.rabbit.listener.SimpleMessageListenerContainer;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

@Configuration(proxyBeanMethods = false)
@ConditionalOnProperty(name = "app.pipeline.enabled", havingValue = "true")
class TeamEventRelayConfiguration {
    private final String queueName = "photoplatform.team.live." + UUID.randomUUID();

    @Bean
    Declarables teamLiveTopology() {
        FanoutExchange exchange = new FanoutExchange(RabbitTeamEventRelay.EXCHANGE, true, false);
        // Amazon MQ 4.3 can default to quorum queues. Exclusive, ephemeral queues must be classic.
        Queue queue = new Queue(queueName, false, true, true, Map.of(
                "x-queue-type", "classic",
                "x-message-ttl", RabbitTeamEventRelay.MESSAGE_TTL_MS,
                "x-expires", 120_000,
                "x-max-length", 1000,
                "x-max-length-bytes", 4_194_304));
        Binding binding = new Binding(queueName, Binding.DestinationType.QUEUE,
                exchange.getName(), "", Map.of());
        return new Declarables(exchange, queue, binding);
    }

    @Bean(destroyMethod = "close")
    RabbitTeamEventRelay teamEventRelay(ObjectMapper mapper, ConnectionFactory connectionFactory) {
        RabbitTemplate rabbit = new RabbitTemplate(connectionFactory);
        rabbit.setMandatory(false);
        rabbit.setUsePublisherConnection(true);
        return new RabbitTeamEventRelay(mapper, rabbit);
    }

    @Bean
    SimpleMessageListenerContainer teamLiveListener(ConnectionFactory connectionFactory, RabbitTeamEventRelay relay) {
        SimpleMessageListenerContainer listener = new SimpleMessageListenerContainer(connectionFactory);
        listener.setQueueNames(queueName);
        listener.setMessageListener((org.springframework.amqp.core.MessageListener) relay::receive);
        listener.setMissingQueuesFatal(false);
        listener.setDefaultRequeueRejected(false);
        listener.setPrefetchCount(25);
        listener.setConcurrentConsumers(1);
        listener.setRecoveryInterval(10_000);
        listener.setShutdownTimeout(1000);
        return listener;
    }
}
