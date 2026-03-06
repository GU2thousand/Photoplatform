package com.generatecloud.app.dto;

import com.generatecloud.app.entity.enums.TeamRole;

public record TeamMemberResponse(
        Long id,
        String name,
        String email,
        TeamRole teamRole
) {
}
