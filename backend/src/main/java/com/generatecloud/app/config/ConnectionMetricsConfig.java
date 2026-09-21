package com.generatecloud.app.config;

import com.generatecloud.app.websocket.TeamCollaborationWebSocketHandler;
import io.micrometer.core.instrument.binder.MeterBinder;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

@Configuration
public class ConnectionMetricsConfig {
    @Bean
    MeterBinder websocketConnections(TeamCollaborationWebSocketHandler handler) {
        return registry -> registry.gauge("websocket_connections", handler,
                TeamCollaborationWebSocketHandler::activeConnections);
    }
}
