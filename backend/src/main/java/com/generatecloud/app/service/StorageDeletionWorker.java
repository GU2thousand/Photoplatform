package com.generatecloud.app.service;

import com.generatecloud.app.repository.StorageDeletionJobRepository;
import com.generatecloud.app.storage.ObjectStorage;
import java.time.Instant;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Propagation;
import org.springframework.transaction.annotation.Transactional;

@Service
@RequiredArgsConstructor
@Slf4j
public class StorageDeletionWorker {
    private final StorageDeletionJobRepository jobs;
    private final ObjectStorage objectStorage;

    @Transactional(propagation = Propagation.REQUIRES_NEW)
    public void process(Long id) {
        jobs.findForUpdate(id).ifPresent(job -> {
            if (job.getNextAttemptAt().isAfter(Instant.now())) {
                return;
            }
            try {
                objectStorage.deleteObject(job.getObjectKey());
                jobs.delete(job);
            } catch (RuntimeException exception) {
                job.setAttempts(job.getAttempts() + 1);
                long delay = Math.min(3600L, 30L << Math.min(job.getAttempts() - 1, 7));
                job.setNextAttemptAt(Instant.now().plusSeconds(delay));
                log.warn("Storage deletion job {} failed; retry {} scheduled", id, job.getAttempts(), exception);
            }
        });
    }
}
