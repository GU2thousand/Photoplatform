package com.generatecloud.app.dto;

import com.generatecloud.app.entity.enums.ModerationStatus;
import com.generatecloud.app.entity.enums.Visibility;
import java.time.Instant;
import java.util.List;

public record ImageResponse(
        Long id,
        String title,
        String description,
        String category,
        List<String> tags,
        String imageUrl,
        String thumbnailUrl,
        Visibility visibility,
        ModerationStatus moderationStatus,
        Long teamId,
        String teamName,
        UserProfileResponse uploader,
        Instant createdAt
) {
}
