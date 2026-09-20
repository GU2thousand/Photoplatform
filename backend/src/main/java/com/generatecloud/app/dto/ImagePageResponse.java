package com.generatecloud.app.dto;

import java.util.List;

public record ImagePageResponse(
        List<ImageResponse> items, int page, int size, long totalElements, int totalPages
) {
}
