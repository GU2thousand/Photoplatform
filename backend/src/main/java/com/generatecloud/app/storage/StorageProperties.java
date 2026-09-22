package com.generatecloud.app.storage;

import lombok.Getter;
import lombok.Setter;
import org.springframework.boot.context.properties.ConfigurationProperties;

@Getter
@Setter
@ConfigurationProperties(prefix = "app.storage")
public class StorageProperties {

    private String provider = "minio";
    private String root = "./data/storage";
    private String bucket = "generatecloud-assets";
    private String region = "us-east-1";
    private String endpoint = "";
    private String accessKey = "";
    private String secretKey = "";
    private boolean pathStyleAccess = true;
    private boolean autoCreateBucket = true;
    private String prefix = "generate-cloud";

    public String qualify(String key) {
        String normalizedPrefix = prefix == null ? "" : prefix.trim().replaceAll("^/+|/+$", "");
        String normalizedKey = key.replaceAll("^/+", "");
        return normalizedPrefix.isBlank() ? normalizedKey : normalizedPrefix + "/" + normalizedKey;
    }
}
