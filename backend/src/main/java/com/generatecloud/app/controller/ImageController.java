package com.generatecloud.app.controller;

import com.generatecloud.app.dto.ImageResponse;
import com.generatecloud.app.entity.enums.ModerationStatus;
import com.generatecloud.app.entity.enums.Visibility;
import com.generatecloud.app.security.AppUserPrincipal;
import com.generatecloud.app.service.AuthService;
import com.generatecloud.app.service.ImageService;
import java.util.List;
import lombok.RequiredArgsConstructor;
import org.springframework.http.MediaType;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PatchMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.multipart.MultipartFile;

@RestController
@RequestMapping("/api/images")
@RequiredArgsConstructor
public class ImageController {

    private final ImageService imageService;
    private final AuthService authService;

    @PostMapping(consumes = MediaType.MULTIPART_FORM_DATA_VALUE)
    public ImageResponse upload(
            @AuthenticationPrincipal AppUserPrincipal principal,
            @RequestParam MultipartFile file,
            @RequestParam String title,
            @RequestParam(required = false) String description,
            @RequestParam(required = false) String category,
            @RequestParam(required = false) String tags,
            @RequestParam(defaultValue = "PRIVATE") Visibility visibility,
            @RequestParam(required = false) Long teamId
    ) {
        return imageService.upload(
                authService.requireUser(principal),
                file,
                title,
                description,
                category,
                tags,
                visibility,
                teamId
        );
    }

    @GetMapping("/me")
    public List<ImageResponse> myImages(@AuthenticationPrincipal AppUserPrincipal principal) {
        return imageService.listUserImages(authService.requireUser(principal));
    }

    @DeleteMapping("/{imageId}")
    public void delete(@AuthenticationPrincipal AppUserPrincipal principal, @PathVariable Long imageId) {
        imageService.delete(authService.requireUser(principal), imageId);
    }

    @GetMapping("/pending")
    public List<ImageResponse> pending(@AuthenticationPrincipal AppUserPrincipal principal) {
        authService.requireUser(principal);
        return imageService.listPendingModeration();
    }

    @PatchMapping("/{imageId}/moderation")
    public ImageResponse moderate(
            @AuthenticationPrincipal AppUserPrincipal principal,
            @PathVariable Long imageId,
            @RequestParam ModerationStatus status
    ) {
        return imageService.updateModerationStatus(authService.requireUser(principal), imageId, status);
    }
}
