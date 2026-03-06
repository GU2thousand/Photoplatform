package com.generatecloud.app.dto;

import com.generatecloud.app.entity.enums.Role;

public record UserProfileResponse(
        Long id,
        String name,
        String email,
        Role role
) {
}
