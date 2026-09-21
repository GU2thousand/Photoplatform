package com.generatecloud.app.pipeline;

import com.generatecloud.app.storage.StorageProperties;
import io.micrometer.core.instrument.simple.SimpleMeterRegistry;
import java.net.URI;
import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.security.KeyPair;
import java.security.KeyPairGenerator;
import java.security.Signature;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.Arrays;
import java.util.Base64;
import java.util.HexFormat;
import java.util.Map;
import java.util.stream.Collectors;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/** Offline signature tests; these do not claim that an AWS distribution accepts the URLs. */
class CloudStorageSigningTests {
    private static final Instant NOW = Instant.parse("2026-01-02T03:04:05Z");
    private static final Clock CLOCK = Clock.fixed(NOW, ZoneOffset.UTC);
    private static KeyPair keyPair;
    private static String pem;

    @BeforeAll
    static void generateSigningKey() throws Exception {
        KeyPairGenerator generator = KeyPairGenerator.getInstance("RSA");
        generator.initialize(2048);
        keyPair = generator.generateKeyPair();
        pem = "-----BEGIN PRIVATE KEY-----\n"
                + Base64.getMimeEncoder(64, new byte[]{'\n'}).encodeToString(keyPair.getPrivate().getEncoded())
                + "\n-----END PRIVATE KEY-----";
    }

    @Test
    void cloudFrontSignsEscapedResourceAndExpiryForBothDeliveryModes() throws Exception {
        StorageProperties storage = new StorageProperties();
        storage.setPrefix(" /photoplatform-dev/ ");
        var signer = new CloudFrontSignedUrlService(storage, pipeline(), CLOCK);
        String key = "media/id/v1/日落 image%?#.webp";
        String signed = signer.download(key, true);
        assertThat(signer.download(key, false)).isEqualTo(signed);
        URI uri = URI.create(signed);
        assertThat(uri.getHost()).isEqualTo("d123example.cloudfront.net");
        assertThat(uri.getScheme()).isEqualTo("https");
        assertThat(uri.getFragment()).isNull();
        assertThat(uri.getPath()).isEqualTo("/photoplatform-dev/" + key);
        assertThat(uri.getRawPath()).contains("%E6%97%A5", "%20", "%25", "%3F", "%23");
        Map<String, String> query = query(uri);
        assertThat(query).containsEntry("Key-Pair-Id", "K123EXAMPLE")
                .containsEntry("Expires", Long.toString(NOW.plusSeconds(60).getEpochSecond()));
        String resource = "https://" + uri.getHost() + uri.getRawPath();
        long expires = Long.parseLong(query.get("Expires"));
        assertThat(verify(resource, expires, query.get("Signature"))).isTrue();
        assertThat(verify(resource, expires + 3600, query.get("Signature"))).isFalse();
        assertThat(verify(resource + "-different-object", expires, query.get("Signature"))).isFalse();
        assertThat(signer.ttlSeconds()).isEqualTo(60);
    }

    @ParameterizedTest
    @ValueSource(strings = {"https://cdn.example.com", "cdn.example.com/media", "cdn..example.com",
            "cdn.-example.com", "cdn.example-.com", "cdn.example.com?key=secret", "localhost", ""})
    void cloudFrontRejectsInvalidHostnames(String domain) {
        PipelineProperties properties = pipeline();
        properties.setCdnDomain(domain);
        assertThatThrownBy(() -> new CloudFrontSignedUrlService(new StorageProperties(), properties, CLOCK))
                .isInstanceOf(IllegalStateException.class)
                .hasMessageNotContaining("secret");
    }

    @ParameterizedTest
    @ValueSource(ints = {0, -1, 301})
    void cloudFrontRejectsUnsafeLifetimes(int ttl) {
        PipelineProperties properties = pipeline();
        properties.setMediaUrlTtlSeconds(ttl);
        assertThatThrownBy(() -> new CloudFrontSignedUrlService(new StorageProperties(), properties, CLOCK))
                .isInstanceOf(IllegalStateException.class);
    }

    @Test
    void cloudFrontRejectsMalformedKeyWithoutRetainingSecretInException() {
        PipelineProperties properties = pipeline();
        properties.setCdnPrivateKeyPem("-----BEGIN PRIVATE KEY-----\nSUPER_SECRET_INVALID_KEY\n-----END PRIVATE KEY-----");
        assertThatThrownBy(() -> new CloudFrontSignedUrlService(new StorageProperties(), properties, CLOCK))
                .isInstanceOf(IllegalStateException.class).hasNoCause()
                .hasMessageNotContaining("SUPER_SECRET_INVALID_KEY");
    }

    @Test
    void cloudFrontRequiresExactlyOneKeySourceAndValidKeyId() {
        PipelineProperties both = pipeline();
        both.setCdnPrivateKeyPath("/secret/path/never-read.pem");
        assertThatThrownBy(() -> new CloudFrontSignedUrlService(new StorageProperties(), both, CLOCK))
                .isInstanceOf(IllegalStateException.class).hasNoCause().hasMessageNotContaining("never-read");
        PipelineProperties neither = pipeline();
        neither.setCdnPrivateKeyPem("");
        assertThatThrownBy(() -> new CloudFrontSignedUrlService(new StorageProperties(), neither, CLOCK))
                .isInstanceOf(IllegalStateException.class);
        PipelineProperties badId = pipeline();
        badId.setCdnKeyPairId("KEY&injected=secret");
        assertThatThrownBy(() -> new CloudFrontSignedUrlService(new StorageProperties(), badId, CLOCK))
                .isInstanceOf(IllegalStateException.class).hasMessageNotContaining("injected");
    }

    @Test
    void directUploadSignatureBindsChecksumMimeLengthSessionAndCreateOnlyCondition() {
        StorageProperties storage = new StorageProperties();
        storage.setProvider("minio");
        storage.setBucket("photoplatform-dev");
        storage.setPrefix("");
        storage.setEndpoint("http://minio:9000");
        PipelineProperties pipeline = new PipelineProperties();
        pipeline.setPublicStorageEndpoint("http://localhost:9000");
        String sha256 = "0123456789abcdef".repeat(4);
        var objectStorage = new PipelineStorage(storage, pipeline, new SimpleMeterRegistry());
        try {
            var upload = objectStorage.signUpload("staging/upload-id/original", "image/jpeg", 1234,
                    sha256, "upload-id", Duration.ofSeconds(120));
            URI uri = URI.create(upload.url());
            assertThat(uri.getHost()).isEqualTo("localhost");
            assertThat(uri.getPath()).isEqualTo("/photoplatform-dev/staging/upload-id/original");
            assertThat(query(uri)).containsEntry("X-Amz-Expires", "120");
            assertThat(query(uri).get("X-Amz-SignedHeaders").split(";"))
                    .contains("content-type", "content-length", "if-none-match", "x-amz-checksum-sha256", "x-amz-meta-upload-id");
            assertThat(upload.headers()).containsEntry("content-type", "image/jpeg")
                    .containsEntry("if-none-match", "*")
                    .containsEntry("x-amz-meta-upload-id", "upload-id")
                    .containsEntry("x-amz-checksum-sha256", Base64.getEncoder().encodeToString(HexFormat.of().parseHex(sha256)));
            // Browser controls these forbidden headers itself; they remain bound by the signature.
            assertThat(upload.headers()).doesNotContainKeys("host", "content-length");
        } finally {
            objectStorage.close();
        }
    }

    private static PipelineProperties pipeline() {
        PipelineProperties properties = new PipelineProperties();
        properties.setCdnDomain("d123example.cloudfront.net");
        properties.setCdnKeyPairId("K123EXAMPLE");
        properties.setCdnPrivateKeyPem(pem);
        properties.setMediaUrlTtlSeconds(60);
        return properties;
    }

    private static Map<String, String> query(URI uri) {
        return Arrays.stream(uri.getRawQuery().split("&")).map(part -> part.split("=", 2))
                .collect(Collectors.toMap(parts -> decode(parts[0]), parts -> decode(parts[1])));
    }

    private static String decode(String value) { return URLDecoder.decode(value, StandardCharsets.UTF_8); }

    private static boolean verify(String resource, long expires, String encodedSignature) throws Exception {
        String policy = "{\"Statement\":[{\"Resource\":\"" + resource
                + "\",\"Condition\":{\"DateLessThan\":{\"AWS:EpochTime\":" + expires + "}}}]}";
        Signature verifier = Signature.getInstance("SHA1withRSA");
        verifier.initVerify(keyPair.getPublic());
        verifier.update(policy.getBytes(StandardCharsets.UTF_8));
        byte[] signature = Base64.getDecoder().decode(encodedSignature.replace('-', '+').replace('_', '=').replace('~', '/'));
        return verifier.verify(signature);
    }
}
