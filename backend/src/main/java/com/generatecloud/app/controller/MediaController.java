package com.generatecloud.app.controller;

import com.generatecloud.app.entity.ImageAsset;
import com.generatecloud.app.entity.enums.ModerationStatus;
import com.generatecloud.app.entity.enums.Visibility;
import com.generatecloud.app.security.AppUserPrincipal;
import com.generatecloud.app.service.AuthService;
import com.generatecloud.app.service.ImageService;
import com.generatecloud.app.service.StorageService;
import com.generatecloud.app.storage.StoredObject;
import lombok.RequiredArgsConstructor;
import org.springframework.core.io.ByteArrayResource;
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
    private final org.springframework.beans.factory.ObjectProvider<com.generatecloud.app.pipeline.DeliveryService> delivery;

    @GetMapping("/{imageId}/url")
    public java.util.Map<String,Object> url(@AuthenticationPrincipal AppUserPrincipal principal,
            @PathVariable Long imageId,
            @org.springframework.web.bind.annotation.RequestParam(defaultValue="original") String variant,
            jakarta.servlet.http.HttpServletResponse response) {
        ImageAsset image = imageService.getAccessibleImage(imageId, authService.optionalUser(principal));
        response.setHeader(HttpHeaders.CACHE_CONTROL,"private, no-store");
        if (!image.getStorageLayout().equals("VERSIONED")) return java.util.Map.of("legacy",true);
        return java.util.Map.of("url",delivery.getObject().url(image,variant),"expiresIn",delivery.getObject().ttlSeconds(),"legacy",false);
    }

    @GetMapping("/{imageId}")
    public ResponseEntity<Resource> original(
            @AuthenticationPrincipal AppUserPrincipal principal,
            @PathVariable Long imageId
    ) {
        ImageAsset image = imageService.getAccessibleImage(imageId, authService.optionalUser(principal));
        return build(image, false);
    }

    @GetMapping("/{imageId}/thumbnail")
    public ResponseEntity<Resource> thumbnail(
            @AuthenticationPrincipal AppUserPrincipal principal,
            @PathVariable Long imageId
    ) {
        ImageAsset image = imageService.getAccessibleImage(imageId, authService.optionalUser(principal));
        return build(image, true);
    }

    private ResponseEntity<Resource> build(ImageAsset image, boolean thumbnail) {
        if (image.getStorageLayout().equals("VERSIONED")) {
            return ResponseEntity.status(302).location(java.net.URI.create(delivery.getObject().url(image,thumbnail?"thumbnail":"original")))
                    .header(HttpHeaders.CACHE_CONTROL,"private, no-store").build();
        }
        StoredObject storedObject = thumbnail
                ? storageService.loadThumbnail(image.getThumbnailFileName())
                : storageService.loadOriginal(image.getStoredFileName());
        MediaType mediaType = resolve(storedObject.contentType());
        Resource resource = new ByteArrayResource(storedObject.content());
        return ResponseEntity.ok()
                .header(HttpHeaders.CACHE_CONTROL,
                        image.getVisibility() == Visibility.PUBLIC
                                && image.getModerationStatus() == ModerationStatus.APPROVED
                                ? "public, max-age=3600" : "private, no-store")
                .header(HttpHeaders.VARY, HttpHeaders.AUTHORIZATION)
                .contentLength(storedObject.contentLength())
                .contentType(mediaType)
                .body(resource);
    }

    private MediaType resolve(String contentType) {
        if (contentType == null || contentType.isBlank()) {
            return MediaType.APPLICATION_OCTET_STREAM;
        }
        MediaType parsed = MediaType.parseMediaType(contentType);
        // Never serve HTML/SVG or other active content from user-controlled filename metadata.
        return java.util.Set.of("image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp", "image/avif")
                .contains(parsed.toString().toLowerCase(java.util.Locale.ROOT))
                ? parsed : MediaType.APPLICATION_OCTET_STREAM;
    }
}
