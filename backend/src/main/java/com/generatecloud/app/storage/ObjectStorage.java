package com.generatecloud.app.storage;

public interface ObjectStorage {

    void putObject(String key, byte[] content, String contentType);

    StoredObject getObject(String key);
}
