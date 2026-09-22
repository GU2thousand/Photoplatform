package com.generatecloud.app.storage;

import jakarta.annotation.PostConstruct;
import jakarta.annotation.PreDestroy;
import java.time.Duration;
import lombok.RequiredArgsConstructor;
import org.springframework.boot.autoconfigure.condition.ConditionalOnExpression;
import org.springframework.stereotype.Service;
import software.amazon.awssdk.core.ResponseBytes;
import software.amazon.awssdk.core.sync.RequestBody;
import software.amazon.awssdk.services.s3.S3Client;
import software.amazon.awssdk.services.s3.model.CreateBucketRequest;
import software.amazon.awssdk.services.s3.model.DeleteObjectRequest;
import software.amazon.awssdk.services.s3.model.GetObjectRequest;
import software.amazon.awssdk.services.s3.model.GetObjectResponse;
import software.amazon.awssdk.services.s3.model.HeadBucketRequest;
import software.amazon.awssdk.services.s3.model.NoSuchBucketException;
import software.amazon.awssdk.services.s3.model.PutObjectRequest;
import software.amazon.awssdk.services.s3.model.S3Exception;

@Service
@RequiredArgsConstructor
@ConditionalOnExpression("'${app.storage.provider:s3}' == 's3' or '${app.storage.provider:s3}' == 'minio' or '${app.storage.provider:s3}' == 'aws'")
public class S3ObjectStorage implements ObjectStorage {

    private final StorageProperties properties;
    private S3Client s3Client;

    @PostConstruct
    void init() {
        s3Client = S3ClientFactory.client(properties);
        if (!S3ClientFactory.isAws(properties) && properties.isAutoCreateBucket()) {
            ensureBucket();
        }
    }

    @PreDestroy
    void close() {
        if (s3Client != null) {
            s3Client.close();
        }
    }

    @Override
    public void putObject(String key, byte[] content, String contentType) {
        PutObjectRequest request = PutObjectRequest.builder()
                .bucket(properties.getBucket())
                .key(properties.qualify(key))
                .contentType(contentType)
                .contentLength((long) content.length)
                .build();
        s3Client.putObject(request, RequestBody.fromBytes(content));
    }

    @Override
    public StoredObject getObject(String key) {
        ResponseBytes<GetObjectResponse> response = s3Client.getObjectAsBytes(GetObjectRequest.builder()
                .bucket(properties.getBucket())
                .key(properties.qualify(key))
                .build());
        return new StoredObject(key, response.response().contentType(), response.asByteArray());
    }

    private void ensureBucket() {
        try {
            s3Client.headBucket(HeadBucketRequest.builder().bucket(properties.getBucket()).build());
        } catch (NoSuchBucketException exception) {
            s3Client.createBucket(CreateBucketRequest.builder().bucket(properties.getBucket()).build());
        } catch (S3Exception exception) {
            if (exception.statusCode() == 404) {
                s3Client.createBucket(CreateBucketRequest.builder().bucket(properties.getBucket()).build());
                return;
            }
            throw exception;
        }
    }

    @Override
    public void deleteObject(String key) {
        s3Client.deleteObject(DeleteObjectRequest.builder()
                .bucket(properties.getBucket()).key(properties.qualify(key))
                .overrideConfiguration(config -> config.apiCallTimeout(Duration.ofSeconds(5))
                        .apiCallAttemptTimeout(Duration.ofSeconds(3)))
                .build());
    }
}
