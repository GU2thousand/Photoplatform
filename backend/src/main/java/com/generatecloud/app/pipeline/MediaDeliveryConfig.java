package com.generatecloud.app.pipeline;

import com.generatecloud.app.storage.StorageProperties;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

@Configuration(proxyBeanMethods = false)
@ConditionalOnProperty(name = "app.pipeline.enabled", havingValue = "true")
public class MediaDeliveryConfig {
    static void validateTtl(int seconds) {
        if (seconds < 1 || seconds > 300)
            throw new IllegalStateException("MEDIA_URL_TTL_SECONDS must be between 1 and 300 seconds");
    }

    @Bean
    MediaUrlService mediaUrlService(StorageProperties storage, PipelineProperties pipeline) {
        validateTtl(pipeline.getMediaUrlTtlSeconds());
        String provider = pipeline.getMediaUrlProvider();
        if ("auto".equals(provider)) provider = pipeline.getCdnDomain().isBlank() ? "s3" : "cloudfront";
        return switch (provider) {
            case "s3" -> new SignedS3UrlService(storage, pipeline);
            case "cloudfront" -> new CloudFrontSignedUrlService(storage, pipeline);
            default -> throw new IllegalStateException("MEDIA_URL_PROVIDER must be s3 or cloudfront");
        };
    }
}
