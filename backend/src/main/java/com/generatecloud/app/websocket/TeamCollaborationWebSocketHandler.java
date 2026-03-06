package com.generatecloud.app.websocket;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.generatecloud.app.dto.TeamEventResponse;
import com.generatecloud.app.entity.UserAccount;
import com.generatecloud.app.repository.TeamMemberRepository;
import com.generatecloud.app.repository.UserAccountRepository;
import com.generatecloud.app.service.JwtService;
import java.io.IOException;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Component;
import org.springframework.web.socket.CloseStatus;
import org.springframework.web.socket.TextMessage;
import org.springframework.web.socket.WebSocketSession;
import org.springframework.web.socket.handler.TextWebSocketHandler;

@Component
@RequiredArgsConstructor
public class TeamCollaborationWebSocketHandler extends TextWebSocketHandler {

    private final ObjectMapper objectMapper;
    private final JwtService jwtService;
    private final UserAccountRepository userAccountRepository;
    private final TeamMemberRepository teamMemberRepository;
    private final Map<Long, Set<WebSocketSession>> sessionsByTeam = new ConcurrentHashMap<>();

    @Override
    public void afterConnectionEstablished(WebSocketSession session) throws Exception {
        SessionBinding binding = bindSession(session.getUri());
        if (binding == null || !jwtService.isTokenValid(binding.token())) {
            session.close(CloseStatus.POLICY_VIOLATION);
            return;
        }

        Optional<UserAccount> userOptional = userAccountRepository.findByEmailIgnoreCase(jwtService.extractEmail(binding.token()));
        if (userOptional.isEmpty()) {
            session.close(CloseStatus.POLICY_VIOLATION);
            return;
        }

        UserAccount user = userOptional.get();
        boolean allowed = user.getRole().name().equals("ADMIN")
                || teamMemberRepository.existsByTeamIdAndUserId(binding.teamId(), user.getId());
        if (!allowed) {
            session.close(CloseStatus.POLICY_VIOLATION);
            return;
        }

        session.getAttributes().put("teamId", binding.teamId());
        session.getAttributes().put("actorName", user.getName());
        sessionsByTeam.computeIfAbsent(binding.teamId(), ignored -> ConcurrentHashMap.newKeySet()).add(session);
    }

    @Override
    protected void handleTextMessage(WebSocketSession session, TextMessage message) throws Exception {
        Long teamId = (Long) session.getAttributes().get("teamId");
        String actorName = (String) session.getAttributes().get("actorName");
        if (teamId == null || actorName == null) {
            session.close(CloseStatus.SERVER_ERROR);
            return;
        }

        String payload = message.getPayload().trim();
        if (payload.isBlank()) {
            return;
        }

        if (payload.length() > 240) {
            payload = payload.substring(0, 240);
        }

        broadcast(teamId, new TeamEventResponse(
                "NOTE",
                actorName + ": " + payload,
                null,
                teamId,
                actorName,
                Instant.now()
        ));
    }

    @Override
    public void afterConnectionClosed(WebSocketSession session, CloseStatus status) {
        Object rawTeamId = session.getAttributes().get("teamId");
        if (rawTeamId instanceof Long teamId) {
            Set<WebSocketSession> teamSessions = sessionsByTeam.get(teamId);
            if (teamSessions != null) {
                teamSessions.remove(session);
            }
        }
    }

    public void broadcast(Long teamId, TeamEventResponse event) {
        Set<WebSocketSession> teamSessions = sessionsByTeam.get(teamId);
        if (teamSessions == null || teamSessions.isEmpty()) {
            return;
        }

        try {
            TextMessage message = new TextMessage(objectMapper.writeValueAsString(event));
            for (WebSocketSession session : teamSessions) {
                if (session.isOpen()) {
                    session.sendMessage(message);
                }
            }
        } catch (IOException ignored) {
        }
    }

    private SessionBinding bindSession(URI uri) {
        if (uri == null || uri.getPath() == null) {
            return null;
        }

        String[] segments = uri.getPath().split("/");
        if (segments.length == 0) {
            return null;
        }

        String rawTeamId = segments[segments.length - 1];
        try {
            long teamId = Long.parseLong(rawTeamId);
            String query = uri.getQuery();
            if (query == null) {
                return null;
            }
            for (String part : query.split("&")) {
                String[] pair = part.split("=", 2);
                if (pair.length == 2 && pair[0].equals("token")) {
                    String token = java.net.URLDecoder.decode(pair[1], StandardCharsets.UTF_8);
                    return new SessionBinding(teamId, token);
                }
            }
            return null;
        } catch (NumberFormatException exception) {
            return null;
        }
    }

    private record SessionBinding(Long teamId, String token) {
    }
}
