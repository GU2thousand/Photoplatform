package com.generatecloud.app.dto;

import jakarta.validation.constraints.Email;
import jakarta.validation.constraints.NotBlank;

public record TeamMemberInviteRequest(
        @Email(message = "Please enter a valid email") @NotBlank(message = "Member email is required") String email
) {
}
