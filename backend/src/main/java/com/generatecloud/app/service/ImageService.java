package com.generatecloud.app.service;

import com.generatecloud.app.dto.ImageResponse;
import com.generatecloud.app.dto.ImageAuthorResponse;
import com.generatecloud.app.dto.ImagePageResponse;
import com.generatecloud.app.dto.TeamEventResponse;
import com.generatecloud.app.entity.ImageAsset;
import com.generatecloud.app.entity.TeamSpace;
import com.generatecloud.app.entity.UserAccount;
import com.generatecloud.app.entity.enums.ModerationStatus;
import com.generatecloud.app.entity.enums.Visibility;
import com.generatecloud.app.exception.BadRequestException;
import com.generatecloud.app.exception.NotFoundException;
import com.generatecloud.app.exception.UnauthorizedAccessException;
import com.generatecloud.app.repository.ImageAssetRepository;
import com.generatecloud.app.websocket.TeamCollaborationWebSocketHandler;
import java.time.Instant;
import java.util.Arrays;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Service;
import org.springframework.data.domain.Page;
import org.springframework.data.domain.PageRequest;
import org.springframework.data.domain.Sort;
import org.springframework.data.jpa.domain.Specification;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.web.multipart.MultipartFile;

@Service
@RequiredArgsConstructor
public class ImageService {

    private final ImageAssetRepository imageAssetRepository;
    private final StorageService storageService;
    private final StorageCleanupService storageCleanupService;
    private final TeamService teamService;
    private final TeamCollaborationWebSocketHandler webSocketHandler;

    @Transactional
    public ImageResponse upload(
            UserAccount actor,
            MultipartFile file,
            String title,
            String description,
            String category,
            String tags,
            Visibility visibility,
            Long teamId
    ) {
        if (title == null || title.isBlank()) {
            throw new BadRequestException("Image title is required");
        }

        if (visibility == null) {
            visibility = Visibility.PRIVATE;
        }

        TeamSpace team = null;
        if (visibility == Visibility.TEAM) {
            if (teamId == null) {
                throw new BadRequestException("Team uploads require a team");
            }
            teamService.requireMembership(teamId, actor);
            team = teamService.getTeam(teamId);
        }

        StorageService.StoredImage storedImage = storageService.store(file);
        ModerationStatus moderationStatus = visibility == Visibility.PUBLIC && actor.getRole().name().equals("USER")
                ? ModerationStatus.PENDING
                : ModerationStatus.APPROVED;

        ImageAsset image = imageAssetRepository.save(ImageAsset.builder()
                .title(title.trim())
                .description(blankToDefault(description, "Uploaded image asset"))
                .category(blankToDefault(category, "General"))
                .tags(normalizeTags(tags))
                .originalFileName(storedImage.getOriginalFileName())
                .storedFileName(storedImage.getStoredFileName())
                .thumbnailFileName(storedImage.getThumbnailFileName())
                .sizeBytes(storedImage.getSizeBytes())
                .visibility(visibility)
                .moderationStatus(moderationStatus)
                .uploader(actor)
                .team(team)
                .build());

        if (team != null) {
            webSocketHandler.broadcast(team.getId(), new TeamEventResponse(
                    "IMAGE_UPLOADED",
                    actor.getName() + " uploaded " + image.getTitle(),
                    image.getId(),
                    team.getId(),
                    actor.getName(),
                    Instant.now()
            ));
        }

        return toResponse(image);
    }

    @Transactional(readOnly = true)
    public ImagePageResponse listPublicImages(String query, String tag, int page, int size) {
        if (page < 0 || size < 1 || size > 100 || (long) page * size > Integer.MAX_VALUE) {
            throw new BadRequestException("Page must be non-negative and size must be between 1 and 100");
        }
        Specification<ImageAsset> specification = (root, criteriaQuery, builder) -> {
            var predicates = new ArrayList<jakarta.persistence.criteria.Predicate>();
            predicates.add(builder.equal(root.get("visibility"), Visibility.PUBLIC));
            predicates.add(builder.equal(root.get("moderationStatus"), ModerationStatus.APPROVED));
            if (query != null && !query.isBlank()) {
                String pattern = "%" + escapeLike(query.trim().toLowerCase(Locale.ROOT)) + "%";
                predicates.add(builder.or(
                        builder.like(builder.lower(root.get("title")), pattern, '\\'),
                        builder.like(builder.lower(root.get("description")), pattern, '\\'),
                        builder.like(builder.lower(root.get("category")), pattern, '\\'),
                        builder.like(builder.lower(root.get("tags")), pattern, '\\')));
            }
            if (tag != null && !tag.isBlank()) {
                // Tags are stored as normalized comma-separated values; delimiters enforce exact matches.
                String normalized = tag.trim().toLowerCase(Locale.ROOT);
                predicates.add(normalized.contains(",") ? builder.disjunction()
                        : builder.like(builder.concat(builder.concat(",", builder.lower(root.get("tags"))), ","),
                                "%," + escapeLike(normalized) + ",%", '\\'));
            }
            return builder.and(predicates.toArray(jakarta.persistence.criteria.Predicate[]::new));
        };
        Page<ImageAsset> result = imageAssetRepository.findAll(specification,
                PageRequest.of(page, size, Sort.by(Sort.Direction.DESC, "createdAt", "id")));
        return new ImagePageResponse(result.map(this::toResponse).getContent(), page, size,
                result.getTotalElements(), result.getTotalPages());
    }

    @Transactional(readOnly = true)
    public List<ImageResponse> listUserImages(UserAccount actor) {
        return imageAssetRepository.findByUploaderIdOrderByCreatedAtDesc(actor.getId()).stream()
                .map(this::toResponse)
                .toList();
    }

    @Transactional(readOnly = true)
    public List<ImageResponse> listTeamImages(UserAccount actor, Long teamId) {
        teamService.requireMembership(teamId, actor);
        return imageAssetRepository.findByTeamIdOrderByCreatedAtDesc(teamId).stream()
                .filter(image -> image.getVisibility() == Visibility.TEAM)
                .map(this::toResponse)
                .toList();
    }

    @Transactional(readOnly = true)
    public List<ImageResponse> listPendingModeration() {
        return imageAssetRepository.findByModerationStatusOrderByCreatedAtDesc(ModerationStatus.PENDING).stream()
                .map(this::toResponse)
                .toList();
    }

    @Transactional
    public ImageResponse updateModerationStatus(UserAccount actor, Long imageId, ModerationStatus status) {
        if (actor.getRole().name().equals("USER")) {
            throw new UnauthorizedAccessException("Only admins can moderate images");
        }
        ImageAsset image = getImage(imageId);
        image.setModerationStatus(status);
        return toResponse(image);
    }

    @Transactional
    public void delete(UserAccount actor, Long imageId) {
        ImageAsset image = getImage(imageId);
        boolean allowed = actor.getRole().name().equals("ADMIN") || image.getUploader().getId().equals(actor.getId());
        if (!allowed) {
            throw new UnauthorizedAccessException("Only the uploader or an admin can delete this image");
        }
        // The outbox and metadata removal commit together. Objects are removed only after commit.
        storageCleanupService.enqueue(image.getStoredFileName(), image.getThumbnailFileName());
        imageAssetRepository.delete(image);
    }

    @Transactional(readOnly = true)
    public ImageAsset getAccessibleImage(Long imageId, UserAccount actor) {
        ImageAsset image = getImage(imageId);
        if (image.getVisibility() == Visibility.PUBLIC && image.getModerationStatus() == ModerationStatus.APPROVED) {
            return image;
        }
        if (actor == null) {
            throw new UnauthorizedAccessException("You do not have access to this image");
        }
        if (actor.getRole().name().equals("ADMIN")) {
            return image;
        }
        if (image.getUploader().getId().equals(actor.getId())) {
            return image;
        }
        if (image.getVisibility() == Visibility.TEAM && image.getTeam() != null
                && teamService.isTeamMember(image.getTeam().getId(), actor.getId())) {
            return image;
        }
        throw new UnauthorizedAccessException("You do not have access to this image");
    }

    public ImageResponse toResponse(ImageAsset image) {
        TeamSpace team = image.getTeam();
        ImageAuthorResponse uploader = new ImageAuthorResponse(image.getUploader().getId(), image.getUploader().getName());
        return new ImageResponse(
                image.getId(),
                image.getTitle(),
                image.getDescription(),
                image.getCategory(),
                splitTags(image.getTags()),
                "/api/files/" + image.getId(),
                "/api/files/" + image.getId() + "/thumbnail",
                image.getVisibility(),
                image.getModerationStatus(),
                team == null ? null : team.getId(),
                team == null ? null : team.getName(),
                uploader,
                image.getCreatedAt()
        );
    }

    public ImageAsset getImage(Long imageId) {
        return imageAssetRepository.findById(imageId)
                .orElseThrow(() -> new NotFoundException("Image not found"));
    }

    private String escapeLike(String value) {
        return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_");
    }

    private String blankToDefault(String value, String fallback) {
        return value == null || value.isBlank() ? fallback : value.trim();
    }

    private String normalizeTags(String tags) {
        if (tags == null || tags.isBlank()) {
            return "general";
        }
        return Arrays.stream(tags.split(","))
                .map(String::trim)
                .filter(value -> !value.isBlank())
                .map(value -> value.toLowerCase(Locale.ROOT))
                .distinct()
                .reduce((left, right) -> left + "," + right)
                .orElse("general");
    }

    private List<String> splitTags(String tags) {
        return Arrays.stream(tags.split(","))
                .map(String::trim)
                .filter(value -> !value.isBlank())
                .toList();
    }
}
