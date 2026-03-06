package com.generatecloud.app.controller;

import com.generatecloud.app.dto.AuthResponse;
import com.generatecloud.app.dto.LoginRequest;
import com.generatecloud.app.dto.RegisterRequest;
import com.generatecloud.app.dto.UserProfileResponse;
import com.generatecloud.app.security.AppUserPrincipal;
import com.generatecloud.app.service.AuthService;
import jakarta.validation.Valid;
import lombok.RequiredArgsConstructor;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api/auth")
@RequiredArgsConstructor
public class AuthController {

    private final AuthService authService;

    @PostMapping("/register")
    public AuthResponse register(@Valid @RequestBody RegisterRequest request) {
        return authService.register(request);
    }

    @PostMapping("/login")
    public AuthResponse login(@Valid @RequestBody LoginRequest request) {
        return authService.login(request);
    }

    @GetMapping("/me")
    public UserProfileResponse me(@AuthenticationPrincipal AppUserPrincipal principal) {
        return authService.me(principal);
    }
}
