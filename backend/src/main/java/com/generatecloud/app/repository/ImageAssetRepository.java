package com.generatecloud.app.repository;

import com.generatecloud.app.entity.ImageAsset;
import com.generatecloud.app.entity.enums.ModerationStatus;
import com.generatecloud.app.entity.enums.Visibility;
import java.util.List;
import org.springframework.data.jpa.repository.JpaRepository;

public interface ImageAssetRepository extends JpaRepository<ImageAsset, Long> {
    List<ImageAsset> findByVisibilityAndModerationStatusOrderByCreatedAtDesc(
            Visibility visibility,
            ModerationStatus moderationStatus
    );

    List<ImageAsset> findByUploaderIdOrderByCreatedAtDesc(Long uploaderId);

    List<ImageAsset> findByTeamIdOrderByCreatedAtDesc(Long teamId);

    List<ImageAsset> findByModerationStatusOrderByCreatedAtDesc(ModerationStatus moderationStatus);

    long countByVisibilityAndModerationStatus(Visibility visibility, ModerationStatus moderationStatus);
}
