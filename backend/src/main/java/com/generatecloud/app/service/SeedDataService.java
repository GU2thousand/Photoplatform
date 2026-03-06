package com.generatecloud.app.service;

import com.generatecloud.app.entity.ImageAsset;
import com.generatecloud.app.entity.TeamMember;
import com.generatecloud.app.entity.TeamSpace;
import com.generatecloud.app.entity.UserAccount;
import com.generatecloud.app.entity.enums.ModerationStatus;
import com.generatecloud.app.entity.enums.Role;
import com.generatecloud.app.entity.enums.TeamRole;
import com.generatecloud.app.entity.enums.Visibility;
import com.generatecloud.app.repository.ImageAssetRepository;
import com.generatecloud.app.repository.TeamMemberRepository;
import com.generatecloud.app.repository.TeamSpaceRepository;
import com.generatecloud.app.repository.UserAccountRepository;
import java.awt.Color;
import lombok.RequiredArgsConstructor;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.boot.CommandLineRunner;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.stereotype.Component;

@Component
@ConditionalOnProperty(name = "app.seed.enabled", havingValue = "true", matchIfMissing = true)
@RequiredArgsConstructor
public class SeedDataService implements CommandLineRunner {

    private final UserAccountRepository userAccountRepository;
    private final TeamSpaceRepository teamSpaceRepository;
    private final TeamMemberRepository teamMemberRepository;
    private final ImageAssetRepository imageAssetRepository;
    private final StorageService storageService;
    private final PasswordEncoder passwordEncoder;

    @Override
    public void run(String... args) {
        if (userAccountRepository.count() > 0) {
            return;
        }

        UserAccount admin = userAccountRepository.save(UserAccount.builder()
                .name("Platform Admin")
                .email("admin@generatecloud.local")
                .passwordHash(passwordEncoder.encode("admin123"))
                .role(Role.ADMIN)
                .build());
        UserAccount creator = userAccountRepository.save(UserAccount.builder()
                .name("Avery Chen")
                .email("avery@generatecloud.local")
                .passwordHash(passwordEncoder.encode("creator123"))
                .role(Role.USER)
                .build());
        UserAccount designer = userAccountRepository.save(UserAccount.builder()
                .name("Sam Rivera")
                .email("sam@generatecloud.local")
                .passwordHash(passwordEncoder.encode("team123"))
                .role(Role.USER)
                .build());

        TeamSpace team = teamSpaceRepository.save(TeamSpace.builder()
                .name("Atlas Studio")
                .description("Shared campaign imagery for launches, brand reviews, and weekly design sync.")
                .build());

        teamMemberRepository.save(TeamMember.builder().team(team).user(creator).teamRole(TeamRole.OWNER).build());
        teamMemberRepository.save(TeamMember.builder().team(team).user(designer).teamRole(TeamRole.MEMBER).build());

        createImage("Launch Still Life", "campaign,hero,product", "Marketing", creator, null,
                Visibility.PUBLIC, ModerationStatus.APPROVED, new Color(24, 78, 119), new Color(238, 155, 0));
        createImage("Texture Library", "reference,material,print", "Reference", creator, null,
                Visibility.PRIVATE, ModerationStatus.APPROVED, new Color(61, 64, 91), new Color(224, 122, 95));
        createImage("Team Moodboard", "team,concept,spring", "Concept", designer, team,
                Visibility.TEAM, ModerationStatus.APPROVED, new Color(48, 71, 94), new Color(144, 190, 109));
        createImage("Pending Street Capture", "urban,editorial,night", "Editorial", creator, null,
                Visibility.PUBLIC, ModerationStatus.PENDING, new Color(17, 24, 39), new Color(99, 102, 241));
    }

    private void createImage(
            String title,
            String tags,
            String category,
            UserAccount uploader,
            TeamSpace team,
            Visibility visibility,
            ModerationStatus moderationStatus,
            Color start,
            Color end
    ) {
        StorageService.StoredImage storedImage = storageService.generateDemoImage(title, start, end);
        imageAssetRepository.save(ImageAsset.builder()
                .title(title)
                .description("Seeded demo image for the intelligent collaborative cloud platform")
                .category(category)
                .tags(tags)
                .originalFileName(storedImage.getOriginalFileName())
                .storedFileName(storedImage.getStoredFileName())
                .thumbnailFileName(storedImage.getThumbnailFileName())
                .sizeBytes(storedImage.getSizeBytes())
                .visibility(visibility)
                .moderationStatus(moderationStatus)
                .uploader(uploader)
                .team(team)
                .build());
    }
}
