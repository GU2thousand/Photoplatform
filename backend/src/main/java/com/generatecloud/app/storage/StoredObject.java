package com.generatecloud.app.storage;

public record StoredObject(
        String key,
        String contentType,
        byte[] content
) {
    public long contentLength() {
        return content.length;
    }
}
