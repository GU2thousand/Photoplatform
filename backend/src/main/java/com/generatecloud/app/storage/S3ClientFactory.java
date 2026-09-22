package com.generatecloud.app.storage;

import java.net.URI;
import java.time.Duration;
import java.util.Set;
import software.amazon.awssdk.auth.credentials.AwsBasicCredentials;
import software.amazon.awssdk.auth.credentials.AwsCredentialsProvider;
import software.amazon.awssdk.auth.credentials.DefaultCredentialsProvider;
import software.amazon.awssdk.auth.credentials.StaticCredentialsProvider;
import software.amazon.awssdk.regions.Region;
import software.amazon.awssdk.services.s3.S3Client;
import software.amazon.awssdk.services.s3.S3Configuration;
import software.amazon.awssdk.services.s3.presigner.S3Presigner;

/** One provider boundary shared by legacy object operations and direct uploads. */
public final class S3ClientFactory {
    private S3ClientFactory() {}

    public static void validate(StorageProperties properties) {
        if (!Set.of("s3", "minio", "aws").contains(properties.getProvider()))
            throw new IllegalStateException("Object storage provider must be aws, minio, or legacy s3");
        if (properties.getBucket() == null || properties.getBucket().isBlank())
            throw new IllegalStateException("STORAGE_BUCKET is required");
        if (isAws(properties) && (hasText(properties.getEndpoint()) || hasText(properties.getAccessKey())
                || hasText(properties.getSecretKey())))
            throw new IllegalStateException("AWS storage uses the default credential chain and regional endpoint; remove STORAGE_ENDPOINT and static STORAGE credentials");
        if (hasText(properties.getAccessKey()) != hasText(properties.getSecretKey()))
            throw new IllegalStateException("Both MinIO storage credentials must be configured together");
    }

    public static boolean isAws(StorageProperties properties) { return "aws".equals(properties.getProvider()); }

    public static AwsCredentialsProvider credentials(StorageProperties properties) {
        validate(properties);
        return (isAws(properties) || ("s3".equals(properties.getProvider()) && !hasText(properties.getAccessKey())))
                ? DefaultCredentialsProvider.builder().build()
                : StaticCredentialsProvider.create(AwsBasicCredentials.create(
                        hasText(properties.getAccessKey()) ? properties.getAccessKey() : "minioadmin",
                        hasText(properties.getSecretKey()) ? properties.getSecretKey() : "minioadmin"));
    }

    public static S3Client client(StorageProperties properties) {
        var builder = S3Client.builder().region(Region.of(properties.getRegion()))
                .credentialsProvider(credentials(properties)).serviceConfiguration(configuration(properties))
                .overrideConfiguration(c -> c.apiCallTimeout(Duration.ofSeconds(10))
                        .apiCallAttemptTimeout(Duration.ofSeconds(4)));
        if (!isAws(properties) && hasText(endpoint(properties))) builder.endpointOverride(URI.create(endpoint(properties)));
        return builder.build();
    }

    public static S3Presigner presigner(StorageProperties properties, String publicEndpoint) {
        if (isAws(properties) && hasText(publicEndpoint))
            throw new IllegalStateException("AWS storage must not set STORAGE_PUBLIC_ENDPOINT");
        var builder = S3Presigner.builder().region(Region.of(properties.getRegion()))
                .credentialsProvider(credentials(properties)).serviceConfiguration(configuration(properties));
        String selectedEndpoint = hasText(publicEndpoint) ? publicEndpoint : endpoint(properties);
        if (!isAws(properties) && hasText(selectedEndpoint)) builder.endpointOverride(URI.create(selectedEndpoint));
        return builder.build();
    }

    private static S3Configuration configuration(StorageProperties properties) {
        return S3Configuration.builder().pathStyleAccessEnabled(!isAws(properties) && properties.isPathStyleAccess()).build();
    }
    private static String endpoint(StorageProperties properties) {
        return hasText(properties.getEndpoint()) ? properties.getEndpoint()
                : "minio".equals(properties.getProvider()) ? "http://localhost:9000" : "";
    }
    private static boolean hasText(String value) { return value != null && !value.isBlank(); }
}
