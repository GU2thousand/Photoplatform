package com.generatecloud.app.dto;

import com.generatecloud.app.entity.enums.ModerationStatus;
import jakarta.validation.constraints.NotNull;

public record ModerationUpdateRequest(
        @NotNull(message = "Moderation status is required") ModerationStatus status
) {
}
