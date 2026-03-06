package com.generatecloud.app.config;

import com.generatecloud.app.websocket.TeamCollaborationWebSocketHandler;
import java.util.Arrays;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Configuration;
import org.springframework.web.socket.config.annotation.EnableWebSocket;
import org.springframework.web.socket.config.annotation.WebSocketConfigurer;
import org.springframework.web.socket.config.annotation.WebSocketHandlerRegistry;

@Configuration
@EnableWebSocket
public class WebSocketConfig implements WebSocketConfigurer {

    private final TeamCollaborationWebSocketHandler handler;
    private final String[] allowedOrigins;

    public WebSocketConfig(
            TeamCollaborationWebSocketHandler handler,
            @Value("${app.cors.allowed-origins}") String allowedOrigins
    ) {
        this.handler = handler;
        this.allowedOrigins = Arrays.stream(allowedOrigins.split(","))
                .map(String::trim)
                .toArray(String[]::new);
    }

    @Override
    public void registerWebSocketHandlers(WebSocketHandlerRegistry registry) {
        registry.addHandler(handler, "/ws/teams/{teamId}")
                .setAllowedOrigins(allowedOrigins);
    }
}
