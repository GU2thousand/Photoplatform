package com.generatecloud.app;

import com.generatecloud.app.entity.*;
import com.generatecloud.app.entity.enums.*;
import com.generatecloud.app.repository.*;
import com.generatecloud.app.service.*;
import com.generatecloud.app.storage.LocalObjectStorage;
import com.generatecloud.app.storage.StorageProperties;
import jakarta.servlet.http.Cookie;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Instant;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.CountDownLatch;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.context.bean.override.mockito.MockitoSpyBean;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.mock.web.MockMultipartFile;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.support.TransactionTemplate;
import org.springframework.util.FileSystemUtils;

import static org.assertj.core.api.Assertions.*;
import static org.awaitility.Awaitility.await;
import static java.util.concurrent.TimeUnit.SECONDS;
import static org.mockito.Mockito.*;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

@SpringBootTest(properties = {
        "spring.datasource.url=jdbc:h2:mem:securityregression;MODE=PostgreSQL;DB_CLOSE_DELAY=-1;DB_CLOSE_ON_EXIT=FALSE",
        "app.storage.root=./build/security-test-storage",
        "app.storage.cleanup-interval-ms=3600000"
})
@AutoConfigureMockMvc
class ImageSecurityIntegrationTests {
    @Autowired MockMvc mvc;
    @Autowired ImageAssetRepository images;
    @Autowired UserAccountRepository users;
    @Autowired TeamSpaceRepository teams;
    @Autowired TeamMemberRepository members;
    @Autowired StorageDeletionJobRepository jobs;
    @Autowired ImageService imageService;
    @Autowired StorageCleanupService cleanup;
    @Autowired JwtService jwt;
    @Autowired StorageProperties storageProperties;
    @Autowired PlatformTransactionManager transactionManager;
    @MockitoSpyBean LocalObjectStorage storage;

    private UserAccount owner;
    private UserAccount outsider;
    private UserAccount admin;

    @BeforeEach
    void fixtures() throws Exception {
        reset(storage);
        jobs.deleteAll();
        images.deleteAll();
        members.deleteAll();
        teams.deleteAll();
        users.deleteAll();
        FileSystemUtils.deleteRecursively(Path.of(storageProperties.getRoot()));
        owner = user("Owner", "owner@example.test", Role.USER);
        outsider = user("Outsider", "outsider@example.test", Role.USER);
        admin = user("Admin", "admin@example.test", Role.ADMIN);
    }

    @Test
    void publicSearchIsPagedExactAndDoesNotLeakAccountDetails() throws Exception {
        for (int index = 0; index < 5; index++) {
            image("Scene " + index, "A quiet scene", "Gallery", "art,photo", Visibility.PUBLIC, ModerationStatus.APPROVED, null);
        }
        image("Unlisted", "Hidden", "Gallery", "art", Visibility.PRIVATE, ModerationStatus.APPROVED, null);
        image("Pending", "Hidden", "Gallery", "art", Visibility.PUBLIC, ModerationStatus.PENDING, null);
        image("Partial", "Different", "Gallery", "cart", Visibility.PUBLIC, ModerationStatus.APPROVED, null);

        mvc.perform(get("/api/public/images").param("tag", " ART ").param("page", "1").param("size", "2"))
                .andExpect(status().isOk()).andExpect(jsonPath("$.items.length()").value(2))
                .andExpect(jsonPath("$.page").value(1)).andExpect(jsonPath("$.size").value(2))
                .andExpect(jsonPath("$.totalElements").value(5)).andExpect(jsonPath("$.totalPages").value(3))
                .andExpect(jsonPath("$.items[0].uploader.id").value(owner.getId()))
                .andExpect(jsonPath("$.items[0].uploader.name").value("Owner"))
                .andExpect(jsonPath("$.items[0].uploader.email").doesNotExist())
                .andExpect(jsonPath("$.items[0].uploader.role").doesNotExist())
                .andExpect(jsonPath("$.items[0].uploader.passwordHash").doesNotExist());
        mvc.perform(get("/api/public/images").param("tag", "art").param("page", "9").param("size", "2"))
                .andExpect(status().isOk()).andExpect(jsonPath("$.items.length()").value(0))
                .andExpect(jsonPath("$.totalElements").value(5));
        mvc.perform(get("/api/public/images").param("tag", "art,photo"))
                .andExpect(status().isOk()).andExpect(jsonPath("$.totalElements").value(0));
    }

    @Test
    void querySearchCoversEveryFieldAndEscapesSqlWildcards() throws Exception {
        image("Blue % sky", "Mountain light", "Travel_Album", "landscape,nature", Visibility.PUBLIC, ModerationStatus.APPROVED, null);
        image("Another image", "Nothing matching", "Other", "different", Visibility.PUBLIC, ModerationStatus.APPROVED, null);
        for (String query : List.of("BLUE", "mountain", "travel", "landscape", "%", "_")) {
            mvc.perform(get("/api/public/images").param("query", query))
                    .andExpect(status().isOk()).andExpect(jsonPath("$.totalElements").value(1));
        }
        mvc.perform(get("/api/public/images").param("page", "-1")).andExpect(status().isBadRequest());
        mvc.perform(get("/api/public/images").param("size", "101")).andExpect(status().isBadRequest());
        mvc.perform(get("/api/public/images").param("size", "0")).andExpect(status().isBadRequest());
        mvc.perform(get("/api/public/images").param("page", "bad")).andExpect(status().isBadRequest());
    }

    @Test
    void mediaCacheAndAccessFollowVisibilityAndModerationForOriginalAndThumbnail() throws Exception {
        TeamSpace team = teams.save(TeamSpace.builder().name("Studio").description("Test studio").build());
        members.save(TeamMember.builder().team(team).user(outsider).teamRole(TeamRole.MEMBER).build());
        ImageAsset publicImage = image("Public", "Approved", "Gallery", "public", Visibility.PUBLIC, ModerationStatus.APPROVED, null);
        ImageAsset privateImage = image("Private", "Personal", "Gallery", "private", Visibility.PRIVATE, ModerationStatus.APPROVED, null);
        ImageAsset pendingImage = image("Pending", "Pending", "Gallery", "pending", Visibility.PUBLIC, ModerationStatus.PENDING, null);
        ImageAsset teamImage = image("Team", "Team", "Gallery", "team", Visibility.TEAM, ModerationStatus.APPROVED, team);
        for (String suffix : List.of("", "/thumbnail")) {
            mvc.perform(get("/api/files/" + publicImage.getId() + suffix))
                    .andExpect(status().isOk()).andExpect(header().string("Cache-Control", "public, max-age=3600"));
            for (ImageAsset protectedImage : List.of(privateImage, pendingImage, teamImage)) {
                mvc.perform(get("/api/files/" + protectedImage.getId() + suffix))
                        .andExpect(status().isForbidden());
                mvc.perform(get("/api/files/" + protectedImage.getId() + suffix).header("Authorization", token(owner)))
                        .andExpect(status().isOk()).andExpect(header().string("Cache-Control", "private, no-store"));
                mvc.perform(get("/api/files/" + protectedImage.getId() + suffix).header("Authorization", token(admin)))
                        .andExpect(status().isOk()).andExpect(header().string("Cache-Control", "private, no-store"));
            }
            mvc.perform(get("/api/files/" + privateImage.getId() + suffix).header("Authorization", token(outsider)))
                    .andExpect(status().isForbidden());
            mvc.perform(get("/api/files/" + pendingImage.getId() + suffix).header("Authorization", token(outsider)))
                    .andExpect(status().isForbidden());
            mvc.perform(get("/api/files/" + teamImage.getId() + suffix).header("Authorization", token(outsider)))
                    .andExpect(status().isOk()).andExpect(header().string("Cache-Control", "private, no-store"));
        }
    }

    @Test
    void bearerIsRequiredAndPendingModerationIsAdminOnly() throws Exception {
        ImageAsset asset = image("Private", "Personal", "Gallery", "private", Visibility.PRIVATE, ModerationStatus.APPROVED, null);
        String rawToken = token(owner).substring(7);
        mvc.perform(get("/api/files/" + asset.getId()).param("token", rawToken)).andExpect(status().isForbidden());
        mvc.perform(get("/api/files/" + asset.getId()).cookie(new Cookie("generate_cloud_token", rawToken)))
                .andExpect(status().isForbidden());
        mvc.perform(get("/api/images/me").cookie(new Cookie("generate_cloud_token", rawToken)))
                .andExpect(status().isForbidden());
        mvc.perform(get("/api/images/pending").header("Authorization", token(owner))).andExpect(status().isForbidden());
        mvc.perform(get("/api/images/pending").header("Authorization", token(admin))).andExpect(status().isOk());
        mvc.perform(patch("/api/images/" + asset.getId() + "/moderation").param("status", "APPROVED")
                        .header("Authorization", token(owner))).andExpect(status().isForbidden());
    }

    @Test
    void misleadingUploadFilenameCannotServeActiveHtmlContent() throws Exception {
        var encoded = new java.io.ByteArrayOutputStream();
        javax.imageio.ImageIO.write(new java.awt.image.BufferedImage(2, 2, java.awt.image.BufferedImage.TYPE_INT_RGB),
                "png", encoded);
        encoded.write("<script>alert('untrusted')</script>".getBytes(java.nio.charset.StandardCharsets.UTF_8));
        mvc.perform(multipart("/api/images")
                        .file(new MockMultipartFile("file", "unsafe.html", "image/png", encoded.toByteArray()))
                        .param("title", "Disguised PNG").header("Authorization", token(owner)))
                .andExpect(status().isOk());
        ImageAsset asset = images.findAll().get(0);
        assertThat(asset.getStoredFileName()).endsWith(".png");
        mvc.perform(get("/api/files/" + asset.getId()).header("Authorization", token(owner)))
                .andExpect(status().isOk()).andExpect(content().contentType("image/png"))
                .andExpect(header().string("X-Content-Type-Options", "nosniff"));

        // Existing pre-upgrade records with unsafe suffixes also must not serve active content.
        String unsafeName = UUID.randomUUID() + ".html";
        storage.putObject("originals/" + unsafeName, encoded.toByteArray(), "image/png");
        asset.setStoredFileName(unsafeName);
        images.save(asset);
        mvc.perform(get("/api/files/" + asset.getId()).header("Authorization", token(owner)))
                .andExpect(status().isOk()).andExpect(content().contentType("application/octet-stream"));
    }

    @Test
    void deletingAnImageRemovesOriginalAndThumbnailAndRejectsOtherUsers() throws Exception {
        ImageAsset asset = image("Delete", "Delete", "Gallery", "delete", Visibility.PRIVATE, ModerationStatus.APPROVED, null);
        mvc.perform(delete("/api/images/" + asset.getId()).header("Authorization", token(outsider)))
                .andExpect(status().isForbidden());
        assertThat(Files.exists(path("originals/" + asset.getStoredFileName()))).isTrue();
        mvc.perform(delete("/api/images/" + asset.getId()).header("Authorization", token(owner)))
                .andExpect(status().isOk());
        assertThat(images.findById(asset.getId())).isEmpty();
        await().atMost(5, SECONDS).untilAsserted(() -> assertThat(jobs.count()).isZero());
        assertThat(Files.exists(path("originals/" + asset.getStoredFileName()))).isFalse();
        assertThat(Files.exists(path("thumbnails/" + asset.getThumbnailFileName()))).isFalse();
        assertThat(jobs.count()).isZero();
        mvc.perform(get("/api/files/" + asset.getId()).header("Authorization", token(owner)))
                .andExpect(status().isNotFound());
    }

    @Test
    void failedStorageDeletionStaysDurableAndRetryRemovesRemainingObject() {
        ImageAsset asset = image("Retry", "Retry", "Gallery", "retry", Visibility.PRIVATE, ModerationStatus.APPROVED, null);
        String originalKey = "originals/" + asset.getStoredFileName();
        doThrow(new IllegalStateException("Temporary storage outage")).when(storage).deleteObject(originalKey);
        imageService.delete(owner, asset.getId());
        assertThat(images.findById(asset.getId())).isEmpty();
        await().atMost(5, SECONDS).untilAsserted(() -> {
            assertThat(jobs.findAll()).singleElement().satisfies(job -> assertThat(job.getAttempts()).isEqualTo(1));
        });
        assertThat(Files.exists(path(originalKey))).isTrue();
        assertThat(Files.exists(path("thumbnails/" + asset.getThumbnailFileName()))).isFalse();
        assertThat(jobs.findAll()).singleElement().satisfies(job -> {
            assertThat(job.getObjectKey()).isEqualTo(originalKey);
            assertThat(job.getAttempts()).isEqualTo(1);
            assertThat(job.getNextAttemptAt()).isAfter(Instant.now());
        });

        doCallRealMethod().when(storage).deleteObject(originalKey);
        StorageDeletionJob job = jobs.findAll().get(0);
        job.setNextAttemptAt(Instant.now().minusSeconds(1));
        jobs.save(job);
        await().atMost(5, SECONDS).untilAsserted(() -> {
            cleanup.retryPendingDeletions();
            assertThat(jobs.count()).isZero();
        });
        assertThat(Files.exists(path(originalKey))).isFalse();
        assertThat(jobs.count()).isZero();
        cleanup.retryPendingDeletions(); // no duplicate or missing-object failures
        storage.deleteObject(originalKey); // provider deletion is idempotent
    }

    @Test
    void rolledBackMetadataDeletionKeepsBothObjectsAndDoesNotLeaveAJob() {
        ImageAsset asset = image("Rollback", "Rollback", "Gallery", "rollback", Visibility.PRIVATE, ModerationStatus.APPROVED, null);
        assertThatThrownBy(() -> new TransactionTemplate(transactionManager).execute(status -> {
            imageService.delete(owner, asset.getId());
            throw new IllegalStateException("Simulated database transaction failure");
        })).isInstanceOf(IllegalStateException.class);
        assertThat(images.findById(asset.getId())).isPresent();
        assertThat(Files.exists(path("originals/" + asset.getStoredFileName()))).isTrue();
        assertThat(Files.exists(path("thumbnails/" + asset.getThumbnailFileName()))).isTrue();
        assertThat(jobs.count()).isZero();
        verify(storage, never()).deleteObject(anyString());
    }

    @Test
    void slowStorageDoesNotHoldTheDeleteRequestOrItsDatabaseConnection() throws Exception {
        ImageAsset asset = image("Slow", "Slow", "Gallery", "slow", Visibility.PRIVATE, ModerationStatus.APPROVED, null);
        String key = "originals/" + asset.getStoredFileName();
        CountDownLatch started = new CountDownLatch(1);
        CountDownLatch release = new CountDownLatch(1);
        doAnswer(invocation -> {
            started.countDown();
            if (!release.await(5, SECONDS)) {
                throw new IllegalStateException("Simulated provider timeout");
            }
            return invocation.callRealMethod();
        }).when(storage).deleteObject(key);
        try {
            org.junit.jupiter.api.Assertions.assertTimeout(java.time.Duration.ofSeconds(2),
                    () -> imageService.delete(owner, asset.getId()));
            assertThat(started.await(2, SECONDS)).isTrue();
            assertThat(images.findById(asset.getId())).isEmpty();
            assertThat(Files.exists(path(key))).isTrue();
        } finally {
            release.countDown();
        }
        await().atMost(5, SECONDS).untilAsserted(() -> assertThat(jobs.count()).isZero());
        assertThat(Files.exists(path(key))).isFalse();
    }

    private UserAccount user(String name, String email, Role role) {
        return users.save(UserAccount.builder().name(name).email(email).passwordHash("unused-test-hash").role(role).build());
    }

    private String token(UserAccount user) {
        return "Bearer " + jwt.generateToken(user.getEmail(), user.getId(), user.getRole().name());
    }

    private Path path(String key) {
        return Path.of(storageProperties.getRoot()).resolve(storageProperties.qualify(key));
    }

    private ImageAsset image(String title, String description, String category, String tags,
                             Visibility visibility, ModerationStatus status, TeamSpace team) {
        String original = UUID.randomUUID() + ".png";
        String thumbnail = UUID.randomUUID() + ".png";
        byte[] bytes = java.util.Base64.getDecoder().decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII=");
        storage.putObject("originals/" + original, bytes, "image/png");
        storage.putObject("thumbnails/" + thumbnail, bytes, "image/png");
        return images.save(ImageAsset.builder().title(title).description(description).category(category).tags(tags)
                .originalFileName("test.png").storedFileName(original).thumbnailFileName(thumbnail).sizeBytes(bytes.length)
                .visibility(visibility).moderationStatus(status).uploader(owner).team(team).build());
    }
}
