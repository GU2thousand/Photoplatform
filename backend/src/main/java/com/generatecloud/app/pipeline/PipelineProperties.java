package com.generatecloud.app.pipeline;

import lombok.Getter;
import lombok.Setter;
import org.springframework.boot.context.properties.ConfigurationProperties;

@Getter
@Setter
@ConfigurationProperties(prefix = "app.pipeline")
public class PipelineProperties {
    private boolean enabled;
    private int uploadTtlSeconds = 900;
    private long maxBytes = 15 * 1024 * 1024;
    private int maxActiveUploads = 20;
    private String publicStorageEndpoint = "http://localhost:9000";
    private String modelVersion = "clip-vit-b32-openai-v1";
    private boolean searchEnabled;
    private String encoderUrl = "http://encoder:8090";
    private String encoderToken = "";
    private String cdnDomain = "";
    private String cdnKeyPairId = "";
    private String cdnPrivateKeyPath = "";
}
