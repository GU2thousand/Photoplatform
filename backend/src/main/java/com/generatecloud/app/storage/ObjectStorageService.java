package com.generatecloud.app.storage;

import java.time.Duration;
import java.util.Map;

/** Infrastructure-neutral operations used by the asynchronous upload pipeline. */
public interface ObjectStorageService {
    record UploadUrl(String url, Map<String, String> headers) {}
    record Metadata(long contentLength, String contentType, Map<String, String> metadata, String checksumSha256) {}
    UploadUrl signUpload(String key, String type, long size, String sha256, String session, Duration ttl);
    Metadata head(String key);
    void delete(String key);
}
