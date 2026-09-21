package com.generatecloud.app.storage;

import java.net.URI;
import java.time.Duration;
import java.util.List;
import java.util.function.Consumer;
import org.junit.jupiter.api.Test;
import software.amazon.awssdk.auth.credentials.DefaultCredentialsProvider;
import software.amazon.awssdk.auth.credentials.StaticCredentialsProvider;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/** Client construction/signing only: no credential resolution or AWS network calls. */
class S3ClientFactoryTests {
    @Test
    void awsUsesDefaultCredentialChainAndRegionalEndpoint() {
        StorageProperties properties = properties("aws");
        try (DefaultCredentialsProvider credentials = (DefaultCredentialsProvider) S3ClientFactory.credentials(properties);
             var client = S3ClientFactory.client(properties)) {
            assertThat(credentials).isInstanceOf(DefaultCredentialsProvider.class);
            assertThat(client.serviceClientConfiguration().endpointOverride()).isEmpty();
            assertThat(client.serviceClientConfiguration().region().id()).isEqualTo("us-east-1");
        }
    }

    @Test
    void awsRejectsEndpointAndStaticCredentialOverridesWithoutLeakingValues() {
        List<Consumer<StorageProperties>> invalid = List.of(
                properties -> properties.setEndpoint("http://secret-endpoint:9000"),
                properties -> properties.setAccessKey("secret-access-key"),
                properties -> properties.setSecretKey("secret-private-key"));
        for (Consumer<StorageProperties> configure : invalid) {
            StorageProperties properties = properties("aws");
            configure.accept(properties);
            assertThatThrownBy(() -> S3ClientFactory.credentials(properties))
                    .isInstanceOf(IllegalStateException.class)
                    .hasMessageNotContaining("secret-endpoint")
                    .hasMessageNotContaining("secret-access-key")
                    .hasMessageNotContaining("secret-private-key");
        }
        assertThatThrownBy(() -> S3ClientFactory.presigner(properties("aws"), "http://secret-public-endpoint:9000"))
                .isInstanceOf(IllegalStateException.class).hasMessageNotContaining("secret-public-endpoint");
    }

    @Test
    void minioDefaultsRemainUsableWithoutAwsCredentials() {
        StorageProperties properties = properties("minio");
        var credentials = S3ClientFactory.credentials(properties);
        assertThat(credentials).isInstanceOf(StaticCredentialsProvider.class);
        assertThat(credentials.resolveCredentials().accessKeyId()).isEqualTo("minioadmin");
        assertThat(credentials.resolveCredentials().secretAccessKey()).isEqualTo("minioadmin");
        try (var client = S3ClientFactory.client(properties)) {
            assertThat(client.serviceClientConfiguration().endpointOverride()).contains(URI.create("http://localhost:9000"));
        }
    }

    @Test
    void legacyS3RetainsRegionalEndpointAndDefaultCredentialChainWhenUnconfigured() {
        StorageProperties properties = properties("s3");
        try (DefaultCredentialsProvider credentials = (DefaultCredentialsProvider) S3ClientFactory.credentials(properties);
             var client = S3ClientFactory.client(properties)) {
            assertThat(credentials).isInstanceOf(DefaultCredentialsProvider.class);
            assertThat(client.serviceClientConfiguration().endpointOverride()).isEmpty();
        }
    }

    @Test
    void legacyS3StillSupportsExplicitCompatibleEndpointAndCredentials() {
        StorageProperties properties = properties("s3");
        properties.setEndpoint("http://legacy-minio:9000");
        properties.setAccessKey("local-access");
        properties.setSecretKey("local-secret");
        var credentials = S3ClientFactory.credentials(properties);
        assertThat(credentials.resolveCredentials().accessKeyId()).isEqualTo("local-access");
        try (var client = S3ClientFactory.client(properties);
             var signer = S3ClientFactory.presigner(properties, "https://storage.example.test")) {
            assertThat(client.serviceClientConfiguration().endpointOverride()).contains(URI.create("http://legacy-minio:9000"));
            var signed = signer.presignGetObject(request -> request.signatureDuration(Duration.ofSeconds(60))
                    .getObjectRequest(get -> get.bucket(properties.getBucket()).key("media/image/v1/original")));
            assertThat(signed.url().getHost()).isEqualTo("storage.example.test");
            assertThat(signed.url().getPath()).isEqualTo("/photoplatform-dev/media/image/v1/original");
        }
    }

    @Test
    void missingBucketUnsupportedProviderAndPartialCredentialsFailFast() {
        StorageProperties missingBucket = properties("aws");
        missingBucket.setBucket(" ");
        assertThatThrownBy(() -> S3ClientFactory.validate(missingBucket)).isInstanceOf(IllegalStateException.class);
        assertThatThrownBy(() -> S3ClientFactory.validate(properties("unknown"))).isInstanceOf(IllegalStateException.class);
        StorageProperties partial = properties("minio");
        partial.setSecretKey("sensitive-partial-secret");
        assertThatThrownBy(() -> S3ClientFactory.validate(partial))
                .isInstanceOf(IllegalStateException.class).hasMessageNotContaining("sensitive-partial-secret");
    }

    private static StorageProperties properties(String provider) {
        StorageProperties properties = new StorageProperties();
        properties.setProvider(provider);
        properties.setBucket("photoplatform-dev");
        properties.setRegion("us-east-1");
        return properties;
    }
}
