package com.generatecloud.app.service;

import com.generatecloud.app.dto.SocketTicketResponse;
import io.jsonwebtoken.Claims;
import io.jsonwebtoken.Jwts;
import io.jsonwebtoken.io.Decoders;
import io.jsonwebtoken.io.DecodingException;
import io.jsonwebtoken.security.Keys;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.Date;
import java.util.Optional;
import javax.crypto.SecretKey;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.core.env.Environment;
import org.springframework.core.env.Profiles;
import org.springframework.stereotype.Service;

@Service
public class JwtService {

    private static final String DEMO_SECRET = "generate-cloud-demo-secret-key-for-local-development-please-change";
    private static final String ACCESS_PURPOSE = "access";
    private static final String SOCKET_PURPOSE = "team-socket";
    private final SecretKey secretKey;
    private final long expirationMs;

    public JwtService(
            @Value("${app.jwt.secret}") String secret,
            @Value("${app.jwt.expiration-ms}") long expirationMs,
            Environment environment
    ) {
        if (secret == null || secret.isBlank()) {
            throw new IllegalArgumentException("app.jwt.secret must be configured");
        }
        if (environment.acceptsProfiles(Profiles.of("prod", "production"))
                && DEMO_SECRET.equals(secret.trim())) {
            throw new IllegalArgumentException("Configure a unique app.jwt.secret for production");
        }
        this.secretKey = buildKey(secret);
        this.expirationMs = expirationMs;
    }

    public String generateToken(String email, Long userId, String role) {
        Instant now = Instant.now();
        return Jwts.builder()
                .subject(email)
                .claim("purpose", ACCESS_PURPOSE)
                .claim("userId", userId)
                .claim("role", role)
                .issuedAt(Date.from(now))
                .expiration(Date.from(now.plusMillis(expirationMs)))
                .signWith(secretKey)
                .compact();
    }

    public SocketTicketResponse generateSocketTicket(String email, Long userId, Long teamId) {
        Instant now = Instant.now().truncatedTo(ChronoUnit.SECONDS);
        Instant expiresAt = now.plusSeconds(60);
        String ticket = Jwts.builder()
                .subject(email)
                .claim("purpose", SOCKET_PURPOSE)
                .claim("userId", userId)
                .claim("teamId", teamId)
                .issuedAt(Date.from(now))
                .expiration(Date.from(expiresAt))
                .signWith(secretKey)
                .compact();
        return new SocketTicketResponse(ticket, expiresAt);
    }

    public Optional<TokenIdentity> readAccessToken(String token) {
        return readIdentity(token, ACCESS_PURPOSE, null);
    }

    public Optional<TokenIdentity> readSocketTicket(String ticket, Long teamId) {
        if (teamId == null) {
            return Optional.empty();
        }
        return readIdentity(ticket, SOCKET_PURPOSE, teamId);
    }

    public String extractEmail(String token) {
        return readAccessToken(token).orElseThrow(() -> new IllegalArgumentException("Invalid access token")).email();
    }

    public boolean isTokenValid(String token) {
        return readAccessToken(token).isPresent();
    }

    private Optional<TokenIdentity> readIdentity(String token, String purpose, Long teamId) {
        try {
            Claims claims = Jwts.parser().verifyWith(secretKey).build().parseSignedClaims(token).getPayload();
            if (!purpose.equals(claims.get("purpose", String.class))
                    || claims.getExpiration() == null || !claims.getExpiration().after(new Date())
                    || claims.getSubject() == null || claims.getSubject().isBlank()) {
                return Optional.empty();
            }
            Long userId = claims.get("userId", Long.class);
            if (userId == null || userId <= 0
                    || (teamId != null && !teamId.equals(claims.get("teamId", Long.class)))) {
                return Optional.empty();
            }
            return Optional.of(new TokenIdentity(userId, claims.getSubject()));
        } catch (Exception ignored) {
            return Optional.empty();
        }
    }

    private SecretKey buildKey(String secret) {
        byte[] bytes;
        try {
            bytes = Decoders.BASE64.decode(secret);
        } catch (DecodingException exception) {
            bytes = secret.getBytes(StandardCharsets.UTF_8);
        }
        // Keys enforces the 256-bit minimum for both decoded and plain-text secrets.
        return Keys.hmacShaKeyFor(bytes);
    }

    public record TokenIdentity(Long userId, String email) {
    }
}
