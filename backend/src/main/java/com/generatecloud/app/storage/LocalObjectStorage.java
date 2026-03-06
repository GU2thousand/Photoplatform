package com.generatecloud.app.storage;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import lombok.RequiredArgsConstructor;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.stereotype.Service;

@Service
@RequiredArgsConstructor
@ConditionalOnProperty(name = "app.storage.provider", havingValue = "local")
public class LocalObjectStorage implements ObjectStorage {

    private final StorageProperties properties;

    @Override
    public void putObject(String key, byte[] content, String contentType) {
        Path target = resolve(key);
        try {
            Files.createDirectories(target.getParent());
            Files.write(target, content);
        } catch (IOException exception) {
            throw new IllegalStateException("Failed to write object to local storage", exception);
        }
    }

    @Override
    public StoredObject getObject(String key) {
        Path source = resolve(key);
        try {
            String contentType = Files.probeContentType(source);
            return new StoredObject(key, contentType, Files.readAllBytes(source));
        } catch (IOException exception) {
            throw new IllegalStateException("Failed to read object from local storage", exception);
        }
    }

    private Path resolve(String key) {
        return Path.of(properties.getRoot()).resolve(properties.qualify(key));
    }
}
