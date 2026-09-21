package com.generatecloud.app.pipeline;

import com.fasterxml.jackson.databind.ObjectMapper;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.TimeUnit;
import lombok.RequiredArgsConstructor;
import org.springframework.amqp.core.*;
import org.springframework.amqp.rabbit.connection.CorrelationData;
import org.springframework.amqp.rabbit.core.RabbitTemplate;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.context.annotation.Bean;
import org.springframework.stereotype.Component;

@Component
@RequiredArgsConstructor
@ConditionalOnProperty(name = "app.pipeline.enabled", havingValue = "true")
public class RabbitMessagePublisher implements MessagePublisher {
    private final RabbitTemplate rabbit;
    private final ObjectMapper json;

    @Bean
    public Declarables mediaQueues() {
        return new Declarables(new Queue("media.process", true), new Queue("media.embed", true),
                new Queue("media.delete", true), new Queue("media.dlq", true));
    }

    public void publish(String queue, UUID jobId, String traceparent) throws Exception {
        var properties = new MessageProperties();
        properties.setContentType("application/json");
        properties.setMessageId(jobId.toString());
        properties.setDeliveryMode(MessageDeliveryMode.PERSISTENT);
        var correlation = new CorrelationData(UUID.randomUUID().toString());
        rabbit.send("", queue, new Message(json.writeValueAsBytes(Map.of("jobId", jobId.toString(),
                "traceparent", traceparent == null ? "" : traceparent)), properties), correlation);
        var confirm = correlation.getFuture().get(3, TimeUnit.SECONDS);
        if (!confirm.isAck() || correlation.getReturned() != null)
            throw new IllegalStateException("Message was not routed and confirmed");
    }
}
