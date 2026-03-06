package com.generatecloud.app.dto;

public record DashboardStatsResponse(
        long userCount,
        long imageCount,
        long publicImageCount,
        long pendingModerationCount,
        long teamCount
) {
}
