package com.generatecloud.app.repository;

import com.generatecloud.app.entity.ImageAsset;
import com.generatecloud.app.entity.enums.ModerationStatus;
import com.generatecloud.app.entity.enums.Visibility;
import java.util.List;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.JpaSpecificationExecutor;
import org.springframework.data.jpa.repository.EntityGraph;
import org.springframework.data.jpa.domain.Specification;
import org.springframework.data.domain.Page;
import org.springframework.data.domain.Pageable;

public interface ImageAssetRepository extends JpaRepository<ImageAsset, Long>, JpaSpecificationExecutor<ImageAsset> {
    @Override
    @EntityGraph(attributePaths = {"uploader", "team"})
    Page<ImageAsset> findAll(Specification<ImageAsset> specification, Pageable pageable);

    List<ImageAsset> findByVisibilityAndModerationStatusOrderByCreatedAtDesc(
            Visibility visibility,
            ModerationStatus moderationStatus
    );

    List<ImageAsset> findByUploaderIdOrderByCreatedAtDesc(Long uploaderId);

    List<ImageAsset> findByTeamIdOrderByCreatedAtDesc(Long teamId);

    List<ImageAsset> findByModerationStatusOrderByCreatedAtDesc(ModerationStatus moderationStatus);

    long countByVisibilityAndModerationStatus(Visibility visibility, ModerationStatus moderationStatus);
}
