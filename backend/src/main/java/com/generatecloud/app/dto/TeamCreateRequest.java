package com.generatecloud.app.dto;

import jakarta.validation.constraints.NotBlank;

public record TeamCreateRequest(
        @NotBlank(message = "Team name is required") String name,
        @NotBlank(message = "Description is required") String description
) {
}
