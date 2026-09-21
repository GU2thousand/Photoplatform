package com.generatecloud.app.pipeline;

import com.generatecloud.app.security.AppUserPrincipal;
import com.generatecloud.app.service.AuthService;
import jakarta.validation.Valid;
import java.util.UUID;
import lombok.RequiredArgsConstructor;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api/uploads")
@RequiredArgsConstructor
@ConditionalOnProperty(name="app.pipeline.enabled",havingValue="true")
public class UploadController {
    private final UploadService uploads;
    private final AuthService auth;
    @PostMapping
    public UploadService.Response create(@AuthenticationPrincipal AppUserPrincipal user,
            @RequestHeader("Idempotency-Key") UUID key, @Valid @RequestBody UploadService.Request request) {
        return uploads.create(auth.requireUser(user),key,request);
    }
    @PostMapping("/{id}/complete")
    public UploadService.Response complete(@AuthenticationPrincipal AppUserPrincipal user,@PathVariable UUID id) {
        return uploads.complete(id,auth.requireUser(user));
    }
    @GetMapping("/{id}")
    public UploadService.Response get(@AuthenticationPrincipal AppUserPrincipal user,@PathVariable UUID id) {
        return uploads.get(id,auth.requireUser(user));
    }
    @DeleteMapping("/{id}")
    public UploadService.Response abort(@AuthenticationPrincipal AppUserPrincipal user,@PathVariable UUID id) {
        return uploads.abort(id,auth.requireUser(user));
    }
}
