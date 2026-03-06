package com.generatecloud.app.dto;

import java.util.List;

public record TeamSummaryResponse(
        Long id,
        String name,
        String description,
        int memberCount,
        List<TeamMemberResponse> members
) {
}
