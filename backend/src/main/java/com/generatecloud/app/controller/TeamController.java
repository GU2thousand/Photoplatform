package com.generatecloud.app.controller;

import com.generatecloud.app.dto.ImageResponse;
import com.generatecloud.app.dto.TeamCreateRequest;
import com.generatecloud.app.dto.TeamMemberInviteRequest;
import com.generatecloud.app.dto.TeamSummaryResponse;
import com.generatecloud.app.security.AppUserPrincipal;
import com.generatecloud.app.service.AuthService;
import com.generatecloud.app.service.ImageService;
import com.generatecloud.app.service.TeamService;
import jakarta.validation.Valid;
import java.util.List;
import lombok.RequiredArgsConstructor;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api/teams")
@RequiredArgsConstructor
public class TeamController {

    private final TeamService teamService;
    private final ImageService imageService;
    private final AuthService authService;

    @GetMapping
    public List<TeamSummaryResponse> teams(@AuthenticationPrincipal AppUserPrincipal principal) {
        return teamService.listTeamsForUser(authService.requireUser(principal));
    }

    @PostMapping
    public TeamSummaryResponse create(
            @AuthenticationPrincipal AppUserPrincipal principal,
            @Valid @RequestBody TeamCreateRequest request
    ) {
        return teamService.createTeam(authService.requireUser(principal), request);
    }

    @GetMapping("/{teamId}")
    public TeamSummaryResponse detail(
            @AuthenticationPrincipal AppUserPrincipal principal,
            @PathVariable Long teamId
    ) {
        return teamService.getTeamDetails(authService.requireUser(principal), teamId);
    }

    @PostMapping("/{teamId}/members")
    public TeamSummaryResponse invite(
            @AuthenticationPrincipal AppUserPrincipal principal,
            @PathVariable Long teamId,
            @Valid @RequestBody TeamMemberInviteRequest request
    ) {
        return teamService.inviteMember(authService.requireUser(principal), teamId, request);
    }

    @GetMapping("/{teamId}/images")
    public List<ImageResponse> teamImages(
            @AuthenticationPrincipal AppUserPrincipal principal,
            @PathVariable Long teamId
    ) {
        return imageService.listTeamImages(authService.requireUser(principal), teamId);
    }
}
