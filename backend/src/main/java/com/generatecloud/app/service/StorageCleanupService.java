package com.generatecloud.app.service;

import com.generatecloud.app.entity.StorageDeletionJob;
import com.generatecloud.app.repository.StorageDeletionJobRepository;
import java.time.Instant;
import java.util.List;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.Executor;
import org.springframework.beans.factory.annotation.Qualifier;
import lombok.extern.slf4j.Slf4j;
import org.springframework.data.domain.PageRequest;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Propagation;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.transaction.support.TransactionSynchronization;
import org.springframework.transaction.support.TransactionSynchronizationManager;

@Service
@Slf4j
public class StorageCleanupService {
    private final StorageDeletionJobRepository jobs;
    private final StorageDeletionWorker worker;
    private final Executor executor;
    private final Set<Long> inFlight = ConcurrentHashMap.newKeySet();

    public StorageCleanupService(StorageDeletionJobRepository jobs, StorageDeletionWorker worker,
                                 @Qualifier("storageCleanupExecutor") Executor executor) {
        this.jobs = jobs;
        this.worker = worker;
        this.executor = executor;
    }

    @Transactional(propagation = Propagation.MANDATORY)
    public void enqueue(String original, String thumbnail) {
        List<Long> ids = List.of("originals/" + original, "thumbnails/" + thumbnail).stream()
                .map(key -> jobs.save(StorageDeletionJob.builder()
                        .objectKey(key).nextAttemptAt(Instant.now()).build()).getId())
                .toList();
        TransactionSynchronizationManager.registerSynchronization(new TransactionSynchronization() {
            @Override
            public void afterCommit() {
                // Dispatch after commit and return immediately so the caller releases its JDBC
                // connection before workers borrow theirs; concurrent deletes cannot exhaust the pool.
                ids.forEach(StorageCleanupService.this::dispatch);
            }
        });
    }

    @Scheduled(fixedDelayString = "${app.storage.cleanup-interval-ms:30000}",
            initialDelayString = "${app.storage.cleanup-interval-ms:30000}")
    public void retryPendingDeletions() {
        jobs.findDueIds(Instant.now(), PageRequest.of(0, 100)).forEach(this::dispatch);
    }

    private void dispatch(Long id) {
        if (!inFlight.add(id)) {
            return;
        }
        try {
            executor.execute(() -> {
                try {
                    worker.process(id);
                } catch (RuntimeException exception) {
                    log.warn("Could not process storage deletion job {}; retained for retry", id, exception);
                } finally {
                    inFlight.remove(id);
                }
            });
        } catch (RuntimeException exception) {
            inFlight.remove(id);
            log.warn("Could not dispatch storage deletion job {}; retained for retry", id, exception);
        }
    }
}
