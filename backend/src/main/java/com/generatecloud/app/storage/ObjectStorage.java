package com.generatecloud.app.storage;

public interface ObjectStorage {

    void putObject(String key, byte[] content, String contentType);

    StoredObject getObject(String key);

    /** Delete if present; missing objects count as success so queued jobs can be retried. */
    void deleteObject(String key);
}
