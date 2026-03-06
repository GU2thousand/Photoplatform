package com.generatecloud.app.controller;

import com.generatecloud.app.entity.ImageAsset;
import com.generatecloud.app.security.AppUserPrincipal;
import com.generatecloud.app.service.AuthService;
import com.generatecloud.app.service.ImageService;
import com.generatecloud.app.service.StorageService;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import lombok.RequiredArgsConstructor;
import org.springframework.core.io.FileSystemResource;
import org.springframework.core.io.Resource;
import org.springframework.http.HttpHeaders;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api/files")
@RequiredArgsConstructor
public class MediaController {

    private final ImageService imageService;
    private final AuthService authService;
    private final StorageService storageService;

    @GetMapping("/{imageId}")
    public ResponseEntity<Resource> original(
            @AuthenticationPrincipal AppUserPrincipal principal,
            @PathVariable Long imageId
    ) throws IOException {
        ImageAsset image = imageService.getAccessibleImage(imageId, authService.optionalUser(principal));
        return build(image, false);
    }

    @GetMapping("/{imageId}/thumbnail")
    public ResponseEntity<Resource> thumbnail(
            @AuthenticationPrincipal AppUserPrincipal principal,
            @PathVariable Long imageId
    ) throws IOException {
        ImageAsset image = imageService.getAccessibleImage(imageId, authService.optionalUser(principal));
        return build(image, true);
    }

    private ResponseEntity<Resource> build(ImageAsset image, boolean thumbnail)
            throws IOException {
        Path path = thumbnail
                ? storageService.resolveThumbnail(image.getThumbnailFileName())
                : storageService.resolveOriginal(image.getStoredFileName());
        MediaType mediaType = resolve(path);
        Resource resource = new FileSystemResource(path);
        return ResponseEntity.ok()
                .header(HttpHeaders.CACHE_CONTROL, "public, max-age=3600")
                .contentType(mediaType)
                .body(resource);
    }

    private MediaType resolve(Path path) throws IOException {
        String contentType = Files.probeContentType(path);
        if (contentType == null) {
            return MediaType.APPLICATION_OCTET_STREAM;
        }
        return MediaType.parseMediaType(contentType);
    }
}
