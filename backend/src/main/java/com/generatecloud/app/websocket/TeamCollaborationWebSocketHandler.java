package com.generatecloud.app.websocket;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.generatecloud.app.dto.TeamEventResponse;
import com.generatecloud.app.entity.UserAccount;
import com.generatecloud.app.repository.TeamMemberRepository;
import com.generatecloud.app.repository.UserAccountRepository;
import com.generatecloud.app.service.JwtService;
import java.net.URI;
import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;
import org.springframework.transaction.support.TransactionSynchronization;
import org.springframework.transaction.support.TransactionSynchronizationManager;
import org.springframework.web.socket.CloseStatus;
import org.springframework.web.socket.PingMessage;
import org.springframework.web.socket.TextMessage;
import org.springframework.web.socket.WebSocketMessage;
import org.springframework.web.socket.WebSocketSession;
import org.springframework.web.socket.handler.TextWebSocketHandler;

@Component
@RequiredArgsConstructor
@Slf4j
public class TeamCollaborationWebSocketHandler extends TextWebSocketHandler {

    private final ObjectMapper objectMapper;
    private final JwtService jwtService;
    private final UserAccountRepository userAccountRepository;
    private final TeamMemberRepository teamMemberRepository;
    private final Map<Long, Set<WebSocketSession>> sessionsByTeam = new ConcurrentHashMap<>();
    private TeamEventRelay eventRelay;

    // Optional injection preserves the local-only mode and the four-argument constructor.
    @Autowired(required = false)
    void setEventRelay(TeamEventRelay relay) {
        relay.subscribe(this::broadcastLocal);
        this.eventRelay = relay;
    }

    public int activeConnections() {
        return sessionsByTeam.values().stream().mapToInt(Set::size).sum();
    }

    @Override
    public void afterConnectionEstablished(WebSocketSession session) throws Exception {
        SessionBinding binding = bindSession(session.getUri());
        Optional<JwtService.TokenIdentity> identity = binding == null
                ? Optional.empty() : jwtService.readSocketTicket(binding.ticket(), binding.teamId());
        if (identity.isEmpty()) {
            disconnect(session, CloseStatus.POLICY_VIOLATION);
            return;
        }
        JwtService.TokenIdentity authenticated = identity.get();
        Optional<UserAccount> userOptional = userAccountRepository.findById(authenticated.userId())
                .filter(user -> user.getEmail().equalsIgnoreCase(authenticated.email()))
                .filter(user -> isAllowed(binding.teamId(), user));
        if (userOptional.isEmpty()) {
            disconnect(session, CloseStatus.POLICY_VIOLATION);
            return;
        }

        UserAccount user = userOptional.get();
        session.getAttributes().put("teamId", binding.teamId());
        session.getAttributes().put("userId", user.getId());
        session.getAttributes().put("email", user.getEmail());
        session.getAttributes().put("actorName", user.getName());
        sessionsByTeam.compute(binding.teamId(), (teamId, sessions) -> {
            Set<WebSocketSession> current = sessions == null ? ConcurrentHashMap.newKeySet() : sessions;
            current.add(session);
            return current;
        });
    }

    @Override
    protected void handleTextMessage(WebSocketSession session, TextMessage message) throws Exception {
        if (!isAuthorized(session)) {
            disconnect(session, CloseStatus.POLICY_VIOLATION);
            return;
        }
        Long teamId = (Long) session.getAttributes().get("teamId");
        String actorName = (String) session.getAttributes().get("actorName");
        String payload = message.getPayload().trim();
        if (payload.isBlank()) {
            return;
        }
        if (payload.length() > 240) {
            payload = payload.substring(0, 240);
        }
        broadcast(teamId, new TeamEventResponse(
                "NOTE", actorName + ": " + payload, null, teamId, actorName, Instant.now()
        ));
    }

    @Override
    public void afterConnectionClosed(WebSocketSession session, CloseStatus status) {
        removeSession(session);
    }

    @Override
    public void handleTransportError(WebSocketSession session, Throwable exception) {
        disconnect(session, CloseStatus.SERVER_ERROR);
    }

    @Scheduled(fixedDelay = 20000)
    public void heartbeat() {
        sessionsByTeam.forEach((teamId, sessions) -> {
            for (WebSocketSession session : sessions) {
                send(session, new PingMessage());
            }
        });
    }

    public void broadcast(Long teamId, TeamEventResponse event) {
        if (TransactionSynchronizationManager.isActualTransactionActive()
                && TransactionSynchronizationManager.isSynchronizationActive()) {
            TransactionSynchronizationManager.registerSynchronization(new TransactionSynchronization() {
                @Override
                public void afterCommit() {
                    broadcastCommitted(teamId, event);
                }
            });
        } else {
            broadcastCommitted(teamId, event);
        }
    }

    private void broadcastCommitted(Long teamId, TeamEventResponse event) {
        broadcastLocal(teamId, event);
        if (eventRelay != null) {
            try {
                // The relay excludes its own instance on consumption, preventing a local echo.
                eventRelay.publish(teamId, event);
            } catch (RuntimeException exception) {
                // Live notifications are best effort; a broker failure must not fail a committed write.
                log.warn("Team live notification relay unavailable; clients can refresh committed state");
            }
        }
    }

    private void broadcastLocal(Long teamId, TeamEventResponse event) {
        Set<WebSocketSession> teamSessions = sessionsByTeam.get(teamId);
        if (teamSessions == null || teamSessions.isEmpty()) {
            return;
        }
        final TextMessage message;
        try {
            message = new TextMessage(objectMapper.writeValueAsString(event));
        } catch (JsonProcessingException exception) {
            throw new IllegalStateException("Could not serialize team event", exception);
        }
        for (WebSocketSession session : teamSessions) {
            send(session, message);
        }
    }

    private void send(WebSocketSession session, WebSocketMessage<?> message) {
        try {
            if (!isAuthorized(session)) {
                disconnect(session, CloseStatus.POLICY_VIOLATION);
                return;
            }
            // Standard WebSocket sessions do not support concurrent writes. Heartbeats and
            // broadcasts share this lock so an active room cannot corrupt its connection.
            synchronized (session) {
                if (!session.isOpen()) {
                    removeSession(session);
                    return;
                }
                session.sendMessage(message);
            }
        } catch (Exception exception) {
            // One failed client must not prevent delivery to the rest of the room.
            disconnect(session, CloseStatus.SERVER_ERROR);
        }
    }

    private boolean isAuthorized(WebSocketSession session) {
        Object rawTeamId = session.getAttributes().get("teamId");
        Object rawUserId = session.getAttributes().get("userId");
        Object rawEmail = session.getAttributes().get("email");
        if (!(rawTeamId instanceof Long teamId) || !(rawUserId instanceof Long userId)
                || !(rawEmail instanceof String email)) {
            return false;
        }
        // The ticket expires after the handshake; the live account and membership govern
        // an established connection, including revoked access and deleted accounts.
        return userAccountRepository.findById(userId)
                .filter(user -> user.getEmail().equalsIgnoreCase(email))
                .filter(user -> isAllowed(teamId, user))
                .isPresent();
    }

    private boolean isAllowed(Long teamId, UserAccount user) {
        return user.getRole().name().equals("ADMIN")
                || teamMemberRepository.existsByTeamIdAndUserId(teamId, user.getId());
    }

    private void disconnect(WebSocketSession session, CloseStatus status) {
        removeSession(session);
        try {
            synchronized (session) {
                if (session.isOpen()) {
                    session.close(status);
                }
            }
        } catch (Exception ignored) {
            // The dead connection has already been removed from the registry.
        }
    }

    private void removeSession(WebSocketSession session) {
        Object rawTeamId = session.getAttributes().get("teamId");
        if (rawTeamId instanceof Long teamId) {
            sessionsByTeam.computeIfPresent(teamId, (id, sessions) -> {
                sessions.remove(session);
                return sessions.isEmpty() ? null : sessions;
            });
        }
    }

    private SessionBinding bindSession(URI uri) {
        if (uri == null || uri.getPath() == null || uri.getRawQuery() == null) {
            return null;
        }
        try {
            String[] segments = uri.getPath().split("/");
            if (segments.length == 0) {
                return null;
            }
            long teamId = Long.parseLong(segments[segments.length - 1]);
            if (teamId <= 0) {
                return null;
            }
            String ticket = null;
            for (String part : uri.getRawQuery().split("&")) {
                String[] pair = part.split("=", 2);
                if (pair.length == 2 && pair[0].equals("ticket")) {
                    if (ticket != null) {
                        return null;
                    }
                    ticket = URLDecoder.decode(pair[1], StandardCharsets.UTF_8);
                }
            }
            return ticket == null || ticket.isBlank() ? null : new SessionBinding(teamId, ticket);
        } catch (IllegalArgumentException exception) {
            return null;
        }
    }

    private record SessionBinding(Long teamId, String ticket) {
    }
}
