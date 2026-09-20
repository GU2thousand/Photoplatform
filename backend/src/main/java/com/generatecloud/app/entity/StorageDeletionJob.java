package com.generatecloud.app.entity;

import jakarta.persistence.*;
import java.time.Instant;
import lombok.*;

/** Durable outbox: retained until the storage provider confirms idempotent deletion. */
@Entity
@Table(name = "storage_deletion_jobs", indexes = @Index(name = "idx_storage_deletion_due", columnList = "nextAttemptAt"))
@Getter
@Setter
@Builder
@NoArgsConstructor
@AllArgsConstructor
public class StorageDeletionJob {
    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @Column(nullable = false, length = 600)
    private String objectKey;

    @Column(nullable = false)
    private int attempts;

    @Column(nullable = false)
    private Instant nextAttemptAt;
}
