package com.generatecloud.app.pipeline;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.generatecloud.app.storage.ObjectStorageService;
import io.micrometer.core.instrument.simple.SimpleMeterRegistry;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import org.junit.jupiter.api.Test;
import org.springframework.amqp.core.*;
import org.springframework.amqp.rabbit.connection.CorrelationData;
import org.springframework.amqp.rabbit.core.RabbitTemplate;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.transaction.PlatformTransactionManager;
import static org.assertj.core.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

class MessagePublisherTests {
    @Test void publishesPersistentJsonAndRequiresConfirmation() throws Exception {
        var rabbit = mock(RabbitTemplate.class);
        UUID id = UUID.randomUUID();
        doAnswer(call -> {
            Message message = call.getArgument(2);
            CorrelationData correlation = call.getArgument(3);
            assertThat(message.getMessageProperties().getMessageId()).isEqualTo(id.toString());
            assertThat(message.getMessageProperties().getDeliveryMode()).isEqualTo(MessageDeliveryMode.PERSISTENT);
            assertThat(new ObjectMapper().readTree(message.getBody()).get("jobId").asText()).isEqualTo(id.toString());
            correlation.getFuture().complete(new CorrelationData.Confirm(true, null));
            return null;
        }).when(rabbit).send(eq(""), eq("media.process"), any(Message.class), any(CorrelationData.class));
        assertThatCode(() -> new RabbitMessagePublisher(rabbit, new ObjectMapper()).publish("media.process", id, null)).doesNotThrowAnyException();
    }
    @Test void rejectsNackAndReturnedMessagesEvenWhenBrokerAcks() throws Exception {
        for (boolean returned : List.of(false, true)) {
            var rabbit = mock(RabbitTemplate.class);
            doAnswer(call -> {
                CorrelationData correlation = call.getArgument(3);
                if (returned) correlation.setReturned(new ReturnedMessage(call.getArgument(2), 312, "NO_ROUTE", "", "missing"));
                correlation.getFuture().complete(new CorrelationData.Confirm(returned, "nack"));
                return null;
            }).when(rabbit).send(anyString(), anyString(), any(Message.class), any(CorrelationData.class));
            assertThatThrownBy(() -> new RabbitMessagePublisher(rabbit, new ObjectMapper()).publish("missing", UUID.randomUUID(), ""))
                    .isInstanceOf(IllegalStateException.class);
        }
    }
    @Test void brokerOutageLeavesOutboxPendingAndRecoveryMarksOnlyConfirmedAttempt() throws Exception {
        var jdbc = mock(JdbcTemplate.class);
        var publisher = mock(MessagePublisher.class);
        var metrics = new SimpleMeterRegistry();
        UUID id = UUID.randomUUID();
        when(jdbc.queryForList(anyString())).thenReturn(List.of(Map.of("id", id, "job_type", "MEDIA_PROCESS", "status", "QUEUED", "attempt", 0)));
        doThrow(new IllegalStateException("broker unavailable")).doNothing().when(publisher).publish("media.process", id, "");
        var dispatcher = new OutboxDispatcher(jdbc, publisher, metrics, mock(ObjectStorageService.class), mock(PlatformTransactionManager.class));
        dispatcher.dispatch();
        verify(jdbc, never()).update(anyString(), any(), any(), any());
        assertThat(metrics.get("media_publish_failures").counter().count()).isEqualTo(1);
        dispatcher.dispatch();
        verify(jdbc).update(contains("j.status=? AND j.attempt=?"), eq(id), eq("QUEUED"), eq(0));
        assertThat(metrics.get("media_publish_confirmed").counter().count()).isEqualTo(1);
    }
}
