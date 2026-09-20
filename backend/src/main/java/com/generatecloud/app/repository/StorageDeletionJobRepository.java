package com.generatecloud.app.repository;

import com.generatecloud.app.entity.StorageDeletionJob;
import jakarta.persistence.LockModeType;
import java.time.Instant;
import java.util.List;
import java.util.Optional;
import org.springframework.data.domain.Pageable;
import org.springframework.data.jpa.repository.*;
import org.springframework.data.repository.query.Param;

public interface StorageDeletionJobRepository extends JpaRepository<StorageDeletionJob, Long> {
    @Query("select job.id from StorageDeletionJob job where job.nextAttemptAt <= :now order by job.id")
    List<Long> findDueIds(@Param("now") Instant now, Pageable pageable);

    @Lock(LockModeType.PESSIMISTIC_WRITE)
    @Query("select job from StorageDeletionJob job where job.id = :id")
    Optional<StorageDeletionJob> findForUpdate(@Param("id") Long id);
}
