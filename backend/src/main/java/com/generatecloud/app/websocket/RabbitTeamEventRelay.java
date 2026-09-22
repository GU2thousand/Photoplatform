package com.generatecloud.app.websocket;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.generatecloud.app.dto.TeamEventResponse;
import java.time.Duration;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.RejectedExecutionException;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicLong;
import java.util.function.BiConsumer;
import lombok.extern.slf4j.Slf4j;
import org.springframework.amqp.core.Message;
import org.springframework.amqp.core.MessageDeliveryMode;
import org.springframework.amqp.core.MessageProperties;
import org.springframework.amqp.rabbit.core.RabbitTemplate;

/**
 * A bounded, non-durable notification channel between API tasks. Publication never waits on
 * the request thread. Overflow, disconnects, and restarts can lose events; clients refresh REST state.
 */
@Slf4j
final class RabbitTeamEventRelay implements TeamEventRelay, AutoCloseable {
    static final String EXCHANGE = "photoplatform.team.live";
    static final int MESSAGE_TTL_MS = 30_000;
    static final int MAX_MESSAGE_BYTES = 32_768;
    static final int OUTBOUND_CAPACITY = 256;
    private static final int DEDUPLICATION_CAPACITY = 2048;
    private static final long DEDUPLICATION_TTL_MS = Duration.ofMinutes(2).toMillis();

    private final ObjectMapper mapper;
    private final RabbitTemplate rabbit;
    private final ExecutorService publisher;
    private final String instanceId;
    private final Map<UUID, Long> recentlyDelivered = new LinkedHashMap<>();
    private final AtomicLong nextWarningAt = new AtomicLong();
    private volatile BiConsumer<Long, TeamEventResponse> subscriber = (teamId, event) -> { };

    RabbitTeamEventRelay(ObjectMapper mapper, RabbitTemplate rabbit) {
        this(mapper, rabbit, new ThreadPoolExecutor(1, 1, 0L, TimeUnit.MILLISECONDS,
                new ArrayBlockingQueue<>(OUTBOUND_CAPACITY), runnable -> {
                    Thread thread = new Thread(runnable, "team-event-relay-publisher");
                    thread.setDaemon(true);
                    return thread;
                }, new ThreadPoolExecutor.AbortPolicy()), UUID.randomUUID().toString());
    }

    RabbitTeamEventRelay(ObjectMapper mapper, RabbitTemplate rabbit, ExecutorService publisher, String instanceId) {
        this.mapper = mapper;
        this.rabbit = rabbit;
        this.publisher = publisher;
        this.instanceId = instanceId;
    }

    @Override
    public void subscribe(BiConsumer<Long, TeamEventResponse> consumer) {
        this.subscriber = consumer;
    }

    @Override
    public void publish(Long teamId, TeamEventResponse event) {
        long createdAt = System.currentTimeMillis();
        try {
            Envelope envelope = new Envelope(1, UUID.randomUUID(), instanceId, teamId, event, createdAt);
            if (!valid(envelope)) {
                warnDropped();
                return;
            }
            byte[] body = mapper.writeValueAsBytes(envelope);
            if (body.length > MAX_MESSAGE_BYTES) {
                warnDropped();
                return;
            }
            publisher.execute(() -> {
                // Time in the in-process buffer counts toward the live-event lifetime too.
                long remaining = MESSAGE_TTL_MS - (System.currentTimeMillis() - createdAt);
                if (remaining <= 0) {
                    warnDropped();
                    return;
                }
                try {
                    MessageProperties properties = new MessageProperties();
                    properties.setContentType(MessageProperties.CONTENT_TYPE_JSON);
                    properties.setDeliveryMode(MessageDeliveryMode.NON_PERSISTENT);
                    properties.setExpiration(Long.toString(remaining));
                    rabbit.send(EXCHANGE, "", new Message(body, properties));
                } catch (RuntimeException exception) {
                    // Never log exception messages: connection failures may contain broker credentials.
                    warnDropped();
                }
            });
        } catch (RejectedExecutionException exception) {
            warnDropped();
        } catch (Exception exception) {
            warnDropped();
        }
    }

    void receive(Message message) {
        try {
            if (message.getBody().length > MAX_MESSAGE_BYTES) {
                warnDropped();
                return;
            }
            Envelope envelope = mapper.readValue(message.getBody(), Envelope.class);
            if (!valid(envelope) || instanceId.equals(envelope.origin())
                    || System.currentTimeMillis() - envelope.createdAt() > MESSAGE_TTL_MS
                    || envelope.createdAt() > System.currentTimeMillis() + MESSAGE_TTL_MS) {
                return;
            }
            if (firstDelivery(envelope.eventId())) {
                // Handler delivery rechecks current account and team membership for every socket.
                subscriber.accept(envelope.teamId(), envelope.event());
            }
        } catch (Exception exception) {
            // Malformed or failed live events are discarded, never requeued as poison messages.
            warnDropped();
        }
    }

    private boolean valid(Envelope envelope) {
        return envelope != null && envelope.version() == 1 && envelope.eventId() != null
                && envelope.origin() != null && !envelope.origin().isBlank()
                && envelope.teamId() != null && envelope.teamId() > 0 && envelope.event() != null
                && envelope.teamId().equals(envelope.event().teamId());
    }

    private synchronized boolean firstDelivery(UUID eventId) {
        long now = System.currentTimeMillis();
        recentlyDelivered.entrySet().removeIf(entry -> now - entry.getValue() > DEDUPLICATION_TTL_MS);
        if (recentlyDelivered.containsKey(eventId)) {
            return false;
        }
        if (recentlyDelivered.size() >= DEDUPLICATION_CAPACITY) {
            recentlyDelivered.remove(recentlyDelivered.keySet().iterator().next());
        }
        recentlyDelivered.put(eventId, now);
        return true;
    }

    private void warnDropped() {
        long now = System.currentTimeMillis();
        long next = nextWarningAt.get();
        if (now >= next && nextWarningAt.compareAndSet(next, now + 60_000)) {
            log.warn("Team live notification dropped; relay is best effort and clients can refresh committed state");
        }
    }

    @Override
    public void close() {
        publisher.shutdownNow();
    }

    record Envelope(int version, UUID eventId, String origin, Long teamId,
                    TeamEventResponse event, long createdAt) { }
}
