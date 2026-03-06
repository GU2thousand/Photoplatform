package com.generatecloud.app.dto;

import java.time.Instant;

public record TeamEventResponse(
        String type,
        String message,
        Long imageId,
        Long teamId,
        String actorName,
        Instant occurredAt
) {
}
