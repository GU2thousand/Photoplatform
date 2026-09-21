package com.generatecloud.app.pipeline;

/** Called only after ImageService has authorized access to a ready, live asset. */
public interface MediaUrlService {
    String download(String key, boolean approvedPublic);
    int ttlSeconds();
}
