package com.generatecloud.app.service;

import com.generatecloud.app.dto.TeamCreateRequest;
import com.generatecloud.app.dto.TeamEventResponse;
import com.generatecloud.app.dto.TeamMemberInviteRequest;
import com.generatecloud.app.dto.TeamMemberResponse;
import com.generatecloud.app.dto.TeamSummaryResponse;
import com.generatecloud.app.entity.TeamMember;
import com.generatecloud.app.entity.TeamSpace;
import com.generatecloud.app.entity.UserAccount;
import com.generatecloud.app.entity.enums.TeamRole;
import com.generatecloud.app.exception.BadRequestException;
import com.generatecloud.app.exception.NotFoundException;
import com.generatecloud.app.exception.UnauthorizedAccessException;
import com.generatecloud.app.repository.TeamMemberRepository;
import com.generatecloud.app.repository.TeamSpaceRepository;
import com.generatecloud.app.repository.UserAccountRepository;
import com.generatecloud.app.websocket.TeamCollaborationWebSocketHandler;
import java.time.Instant;
import java.util.Comparator;
import java.util.List;
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

@Service
@RequiredArgsConstructor
public class TeamService {

    private final TeamSpaceRepository teamSpaceRepository;
    private final TeamMemberRepository teamMemberRepository;
    private final UserAccountRepository userAccountRepository;
    private final TeamCollaborationWebSocketHandler webSocketHandler;

    @Transactional
    public TeamSummaryResponse createTeam(UserAccount owner, TeamCreateRequest request) {
        TeamSpace team = teamSpaceRepository.save(TeamSpace.builder()
                .name(request.name().trim())
                .description(request.description().trim())
                .build());

        teamMemberRepository.save(TeamMember.builder()
                .team(team)
                .user(owner)
                .teamRole(TeamRole.OWNER)
                .build());

        return toSummary(team);
    }

    @Transactional
    public TeamSummaryResponse inviteMember(UserAccount actor, Long teamId, TeamMemberInviteRequest request) {
        TeamSpace team = getTeam(teamId);
        requireManager(teamId, actor);

        UserAccount member = userAccountRepository.findByEmailIgnoreCase(request.email().trim())
                .orElseThrow(() -> new NotFoundException("No user exists for that email"));

        if (teamMemberRepository.existsByTeamIdAndUserId(teamId, member.getId())) {
            throw new BadRequestException("That user is already in the team");
        }

        teamMemberRepository.save(TeamMember.builder()
                .team(team)
                .user(member)
                .teamRole(TeamRole.MEMBER)
                .build());

        webSocketHandler.broadcast(teamId, new TeamEventResponse(
                "MEMBER_ADDED",
                actor.getName() + " added " + member.getName() + " to the team",
                null,
                teamId,
                actor.getName(),
                Instant.now()
        ));

        return toSummary(team);
    }

    @Transactional(readOnly = true)
    public List<TeamSummaryResponse> listTeamsForUser(UserAccount user) {
        List<TeamSpace> teams = user.getRole().name().equals("ADMIN")
                ? teamSpaceRepository.findAll()
                : teamMemberRepository.findByUserId(user.getId()).stream()
                        .map(TeamMember::getTeam)
                        .toList();

        return teams.stream()
                .distinct()
                .sorted(Comparator.comparing(TeamSpace::getCreatedAt).reversed())
                .map(this::toSummary)
                .toList();
    }

    @Transactional(readOnly = true)
    public TeamSummaryResponse getTeamDetails(UserAccount user, Long teamId) {
        requireMembership(teamId, user);
        return toSummary(getTeam(teamId));
    }

    @Transactional(readOnly = true)
    public boolean isTeamMember(Long teamId, Long userId) {
        return teamMemberRepository.existsByTeamIdAndUserId(teamId, userId);
    }

    @Transactional(readOnly = true)
    public TeamSpace getTeam(Long teamId) {
        return teamSpaceRepository.findById(teamId)
                .orElseThrow(() -> new NotFoundException("Team not found"));
    }

    public void requireMembership(Long teamId, UserAccount user) {
        if (user.getRole().name().equals("ADMIN")) {
            return;
        }
        if (!teamMemberRepository.existsByTeamIdAndUserId(teamId, user.getId())) {
            throw new UnauthorizedAccessException("You do not have access to this team");
        }
    }

    public void requireManager(Long teamId, UserAccount user) {
        if (user.getRole().name().equals("ADMIN")) {
            return;
        }
        TeamMember membership = teamMemberRepository.findByTeamIdAndUserId(teamId, user.getId())
                .orElseThrow(() -> new UnauthorizedAccessException("You do not have access to this team"));
        if (membership.getTeamRole() != TeamRole.OWNER) {
            throw new UnauthorizedAccessException("Only team owners can invite members");
        }
    }

    private TeamSummaryResponse toSummary(TeamSpace team) {
        List<TeamMemberResponse> members = teamMemberRepository.findByTeamId(team.getId()).stream()
                .map(member -> new TeamMemberResponse(
                        member.getUser().getId(),
                        member.getUser().getName(),
                        member.getUser().getEmail(),
                        member.getTeamRole()
                ))
                .sorted(Comparator.comparing(TeamMemberResponse::name))
                .toList();

        return new TeamSummaryResponse(
                team.getId(),
                team.getName(),
                team.getDescription(),
                members.size(),
                members
        );
    }
}
