package com.generatecloud.app.pipeline;

import java.util.UUID;

/** Success means a persistent publication was both routed and broker-confirmed. */
public interface MessagePublisher {
    void publish(String queue, UUID jobId, String traceparent) throws Exception;
}
