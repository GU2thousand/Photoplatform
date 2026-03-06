package com.generatecloud.app.service;

import com.generatecloud.app.dto.DashboardStatsResponse;
import com.generatecloud.app.entity.enums.ModerationStatus;
import com.generatecloud.app.entity.enums.Visibility;
import com.generatecloud.app.repository.ImageAssetRepository;
import com.generatecloud.app.repository.TeamSpaceRepository;
import com.generatecloud.app.repository.UserAccountRepository;
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Service;

@Service
@RequiredArgsConstructor
public class AdminService {

    private final UserAccountRepository userAccountRepository;
    private final ImageAssetRepository imageAssetRepository;
    private final TeamSpaceRepository teamSpaceRepository;

    public DashboardStatsResponse buildStats(boolean includePending) {
        long pending = includePending
                ? imageAssetRepository.findByModerationStatusOrderByCreatedAtDesc(ModerationStatus.PENDING).size()
                : 0;
        return new DashboardStatsResponse(
                userAccountRepository.count(),
                imageAssetRepository.count(),
                imageAssetRepository.countByVisibilityAndModerationStatus(Visibility.PUBLIC, ModerationStatus.APPROVED),
                pending,
                teamSpaceRepository.count()
        );
    }
}
