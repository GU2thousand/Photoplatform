package com.generatecloud.app.controller;

import com.generatecloud.app.dto.DashboardStatsResponse;
import com.generatecloud.app.dto.ImageResponse;
import com.generatecloud.app.dto.ModerationUpdateRequest;
import com.generatecloud.app.security.AppUserPrincipal;
import com.generatecloud.app.service.AdminService;
import com.generatecloud.app.service.AuthService;
import com.generatecloud.app.service.ImageService;
import jakarta.validation.Valid;
import java.util.List;
import lombok.RequiredArgsConstructor;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PatchMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api/admin")
@RequiredArgsConstructor
public class AdminController {

    private final AdminService adminService;
    private final ImageService imageService;
    private final AuthService authService;

    @GetMapping("/stats")
    public DashboardStatsResponse stats(@AuthenticationPrincipal AppUserPrincipal principal) {
        authService.requireUser(principal);
        return adminService.buildStats(true);
    }

    @GetMapping("/images/pending")
    public List<ImageResponse> pending(@AuthenticationPrincipal AppUserPrincipal principal) {
        authService.requireUser(principal);
        return imageService.listPendingModeration();
    }

    @PatchMapping("/images/{imageId}")
    public ImageResponse moderate(
            @AuthenticationPrincipal AppUserPrincipal principal,
            @PathVariable Long imageId,
            @Valid @RequestBody ModerationUpdateRequest request
    ) {
        return imageService.updateModerationStatus(authService.requireUser(principal), imageId, request.status());
    }
}
