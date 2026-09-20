package com.generatecloud.app.security;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.generatecloud.app.dto.SocketTicketResponse;
import com.generatecloud.app.dto.TeamEventResponse;
import com.generatecloud.app.entity.UserAccount;
import com.generatecloud.app.entity.enums.Role;
import com.generatecloud.app.repository.TeamMemberRepository;
import com.generatecloud.app.repository.UserAccountRepository;
import com.generatecloud.app.service.JwtService;
import com.generatecloud.app.websocket.TeamCollaborationWebSocketHandler;
import io.jsonwebtoken.Jwts;
import io.jsonwebtoken.security.Keys;
import jakarta.servlet.FilterChain;
import jakarta.servlet.http.Cookie;
import java.io.IOException;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.util.Date;
import java.util.HashMap;
import java.util.Map;
import java.util.Optional;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.mock.env.MockEnvironment;
import org.springframework.mock.web.MockHttpServletRequest;
import org.springframework.mock.web.MockHttpServletResponse;
import org.springframework.security.core.context.SecurityContextHolder;
import org.springframework.web.socket.CloseStatus;
import org.springframework.web.socket.PingMessage;
import org.springframework.web.socket.TextMessage;
import org.springframework.web.socket.WebSocketSession;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.Mockito.doThrow;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

class SocketSecurityTests {
    private static final String SECRET = "test-secret-key-for-jwt-signing-1234567890";
    private JwtService jwtService;
    private UserAccountRepository users;
    private TeamMemberRepository members;
    private TeamCollaborationWebSocketHandler handler;
    private UserAccount user;

    @BeforeEach
    void setup() {
        jwtService = new JwtService(SECRET, 86400000, new MockEnvironment());
        users = mock(UserAccountRepository.class);
        members = mock(TeamMemberRepository.class);
        handler = new TeamCollaborationWebSocketHandler(new ObjectMapper().findAndRegisterModules(), jwtService, users, members);
        user = UserAccount.builder().id(1L).email("member@example.com").name("Member").role(Role.USER).build();
        when(users.findById(1L)).thenReturn(Optional.of(user));
        when(members.existsByTeamIdAndUserId(10L, 1L)).thenReturn(true);
    }

    @AfterEach
    void cleanupContext() {
        SecurityContextHolder.clearContext();
    }

    @Test
    void ticketsAreShortLivedAndSeparatedFromAccessTokens() {
        Instant before = Instant.now();
        SocketTicketResponse response = jwtService.generateSocketTicket(user.getEmail(), 1L, 10L);
        assertThat(response.expiresAt()).isAfter(before.plusSeconds(58)).isBefore(before.plusSeconds(61));
        assertThat(jwtService.readSocketTicket(response.ticket(), 10L)).contains(new JwtService.TokenIdentity(1L, user.getEmail()));
        assertThat(jwtService.readSocketTicket(response.ticket(), 11L)).isEmpty();
        assertThat(jwtService.readAccessToken(response.ticket())).isEmpty();
        String access = jwtService.generateToken(user.getEmail(), 1L, "USER");
        assertThat(jwtService.readAccessToken(access)).isPresent();
        assertThat(jwtService.readSocketTicket(access, 10L)).isEmpty();
    }

    @Test
    void rejectsExpiredWrongPurposeAndTamperedTickets() {
        assertThat(jwtService.readSocketTicket(signedTicket("team-socket", Instant.now().minusSeconds(1)), 10L)).isEmpty();
        assertThat(jwtService.readSocketTicket(signedTicket("access", Instant.now().plusSeconds(60)), 10L)).isEmpty();
        String valid = jwtService.generateSocketTicket(user.getEmail(), 1L, 10L).ticket();
        String[] parts = valid.split("\\.");
        parts[1] = parts[1].substring(0, parts[1].length() - 1) + "A";
        assertThat(jwtService.readSocketTicket(String.join(".", parts), 10L)).isEmpty();
        assertThat(jwtService.readSocketTicket("not-a-token", 10L)).isEmpty();
    }

    @Test
    void productionRejectsDemoBlankAndWeakSecrets() {
        MockEnvironment production = new MockEnvironment();
        production.setActiveProfiles("prod");
        assertThatThrownBy(() -> new JwtService("generate-cloud-demo-secret-key-for-local-development-please-change", 60000, production))
                .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> new JwtService("  ", 60000, production)).isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> new JwtService("weak-secret", 60000, production)).isInstanceOf(RuntimeException.class);
        assertThat(new JwtService(SECRET, 60000, production).generateToken(user.getEmail(), 1L, "USER")).isNotBlank();
    }

    @Test
    void httpOnlyAuthenticatesAccessBearerTokens() throws Exception {
        JwtAuthenticationFilter filter = new JwtAuthenticationFilter(jwtService, users);
        String access = jwtService.generateToken(user.getEmail(), 1L, "USER");
        MockHttpServletRequest request = new MockHttpServletRequest("GET", "/api/files/test.jpg");
        request.addParameter("token", access);
        request.setCookies(new Cookie("generate_cloud_token", access));
        filter.doFilter(request, new MockHttpServletResponse(), mock(FilterChain.class));
        assertThat(SecurityContextHolder.getContext().getAuthentication()).isNull();
        verify(users, never()).findById(anyLong());

        request = new MockHttpServletRequest();
        request.addHeader("Authorization", "Bearer " + jwtService.generateSocketTicket(user.getEmail(), 1L, 10L).ticket());
        filter.doFilter(request, new MockHttpServletResponse(), mock(FilterChain.class));
        assertThat(SecurityContextHolder.getContext().getAuthentication()).isNull();

        request = new MockHttpServletRequest();
        request.addHeader("Authorization", "Bearer " + access);
        filter.doFilter(request, new MockHttpServletResponse(), mock(FilterChain.class));
        assertThat(SecurityContextHolder.getContext().getAuthentication()).isNotNull();
    }

    @Test
    void websocketRejectsNormalTokensWrongTeamsAndNonmembers() throws Exception {
        WebSocketSession normal = session("token=" + jwtService.generateToken(user.getEmail(), 1L, "USER"));
        handler.afterConnectionEstablished(normal);
        verify(normal).close(CloseStatus.POLICY_VIOLATION);
        WebSocketSession wrongPurpose = session("ticket=" + jwtService.generateToken(user.getEmail(), 1L, "USER"));
        handler.afterConnectionEstablished(wrongPurpose);
        verify(wrongPurpose).close(CloseStatus.POLICY_VIOLATION);
        WebSocketSession wrongTeam = session("ticket=" + jwtService.generateSocketTicket(user.getEmail(), 1L, 11L).ticket());
        handler.afterConnectionEstablished(wrongTeam);
        verify(wrongTeam).close(CloseStatus.POLICY_VIOLATION);
        when(members.existsByTeamIdAndUserId(10L, 1L)).thenReturn(false);
        WebSocketSession nonmember = validSession();
        handler.afterConnectionEstablished(nonmember);
        verify(nonmember).close(CloseStatus.POLICY_VIOLATION);
    }

    @Test
    void heartbeatKeepsAuthorizedConnectionsAndClosesRevokedMembership() throws Exception {
        WebSocketSession session = validSession();
        handler.afterConnectionEstablished(session);
        handler.heartbeat();
        verify(session).sendMessage(any(PingMessage.class));
        when(members.existsByTeamIdAndUserId(10L, 1L)).thenReturn(false);
        handler.heartbeat();
        verify(session).close(CloseStatus.POLICY_VIOLATION);
        when(members.existsByTeamIdAndUserId(10L, 1L)).thenReturn(true);
        handler.heartbeat();
        verify(session, times(1)).sendMessage(any(PingMessage.class));
    }

    @Test
    void failedSendDoesNotBlockOtherClientsAndIsRemoved() throws Exception {
        WebSocketSession failed = validSession();
        WebSocketSession healthy = validSession();
        handler.afterConnectionEstablished(failed);
        handler.afterConnectionEstablished(healthy);
        doThrow(new IOException("client disconnected")).when(failed).sendMessage(any());
        TeamEventResponse event = new TeamEventResponse("NOTE", "hello", null, 10L, "Member", Instant.now());
        handler.broadcast(10L, event);
        verify(failed).close(CloseStatus.SERVER_ERROR);
        verify(healthy).sendMessage(any(TextMessage.class));
        handler.heartbeat();
        verify(failed, times(1)).sendMessage(any());
        verify(healthy).sendMessage(any(PingMessage.class));
        handler.afterConnectionClosed(healthy, CloseStatus.NORMAL);
        handler.heartbeat();
        verify(healthy, times(2)).sendMessage(any());
    }

    private String signedTicket(String purpose, Instant expiration) {
        return Jwts.builder().subject(user.getEmail()).claim("purpose", purpose).claim("userId", 1L)
                .claim("teamId", 10L).expiration(Date.from(expiration))
                .signWith(Keys.hmacShaKeyFor(SECRET.getBytes(StandardCharsets.UTF_8))).compact();
    }

    private WebSocketSession validSession() {
        return session("ticket=" + jwtService.generateSocketTicket(user.getEmail(), 1L, 10L).ticket());
    }

    private WebSocketSession session(String query) {
        WebSocketSession session = mock(WebSocketSession.class);
        Map<String, Object> attributes = new HashMap<>();
        when(session.getAttributes()).thenReturn(attributes);
        when(session.getUri()).thenReturn(URI.create("ws://localhost/ws/teams/10?" + query));
        when(session.isOpen()).thenReturn(true);
        return session;
    }
}
