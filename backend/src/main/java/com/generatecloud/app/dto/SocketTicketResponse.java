package com.generatecloud.app.dto;

import java.time.Instant;

public record SocketTicketResponse(String ticket, Instant expiresAt) {
}
