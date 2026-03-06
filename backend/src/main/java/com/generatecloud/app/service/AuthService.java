package com.generatecloud.app.service;

import com.generatecloud.app.dto.AuthResponse;
import com.generatecloud.app.dto.LoginRequest;
import com.generatecloud.app.dto.RegisterRequest;
import com.generatecloud.app.dto.UserProfileResponse;
import com.generatecloud.app.entity.UserAccount;
import com.generatecloud.app.entity.enums.Role;
import com.generatecloud.app.exception.BadRequestException;
import com.generatecloud.app.exception.NotFoundException;
import com.generatecloud.app.exception.UnauthorizedAccessException;
import com.generatecloud.app.repository.UserAccountRepository;
import com.generatecloud.app.security.AppUserPrincipal;
import java.util.Locale;
import lombok.RequiredArgsConstructor;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.stereotype.Service;

@Service
@RequiredArgsConstructor
public class AuthService {

    private final UserAccountRepository userAccountRepository;
    private final PasswordEncoder passwordEncoder;
    private final JwtService jwtService;

    public AuthResponse register(RegisterRequest request) {
        String normalizedEmail = request.email().trim().toLowerCase(Locale.ROOT);
        if (userAccountRepository.existsByEmailIgnoreCase(normalizedEmail)) {
            throw new BadRequestException("Email already exists");
        }

        UserAccount user = userAccountRepository.save(UserAccount.builder()
                .name(request.name().trim())
                .email(normalizedEmail)
                .passwordHash(passwordEncoder.encode(request.password()))
                .role(Role.USER)
                .build());
        return toAuthResponse(user);
    }

    public AuthResponse login(LoginRequest request) {
        UserAccount user = userAccountRepository.findByEmailIgnoreCase(request.email().trim())
                .orElseThrow(() -> new UnauthorizedAccessException("Invalid email or password"));
        if (!passwordEncoder.matches(request.password(), user.getPasswordHash())) {
            throw new UnauthorizedAccessException("Invalid email or password");
        }
        return toAuthResponse(user);
    }

    public UserProfileResponse me(AppUserPrincipal principal) {
        return toProfile(requireUser(principal));
    }

    public UserAccount requireUser(AppUserPrincipal principal) {
        if (principal == null) {
            throw new UnauthorizedAccessException("Authentication is required");
        }
        return userAccountRepository.findById(principal.id())
                .orElseThrow(() -> new NotFoundException("User not found"));
    }

    public UserAccount optionalUser(AppUserPrincipal principal) {
        return principal == null ? null : requireUser(principal);
    }

    public UserProfileResponse toProfile(UserAccount user) {
        return new UserProfileResponse(user.getId(), user.getName(), user.getEmail(), user.getRole());
    }

    private AuthResponse toAuthResponse(UserAccount user) {
        String token = jwtService.generateToken(user.getEmail(), user.getId(), user.getRole().name());
        return new AuthResponse(token, toProfile(user));
    }
}
