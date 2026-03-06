package com.generatecloud.app.dto;

public record AuthResponse(
        String token,
        UserProfileResponse user
) {
}
