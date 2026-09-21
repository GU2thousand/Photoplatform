package com.generatecloud.app.pipeline;

import com.generatecloud.app.dto.ImageResponse;
import com.generatecloud.app.entity.UserAccount;
import com.generatecloud.app.entity.enums.Role;
import com.generatecloud.app.exception.BadRequestException;
import com.generatecloud.app.service.ImageService;
import io.micrometer.core.instrument.MeterRegistry;
import java.net.URI;
import java.net.http.*;
import java.time.Duration;
import java.util.*;
import com.fasterxml.jackson.databind.ObjectMapper;
import lombok.RequiredArgsConstructor;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.http.HttpStatus;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.web.server.ResponseStatusException;

@Service
@RequiredArgsConstructor
@ConditionalOnProperty(name="app.pipeline.enabled",havingValue="true")
public class SearchService {
    private final NamedParameterJdbcTemplate jdbc;
    private final ImageService images;
    private final PipelineProperties properties;
    private final MeterRegistry metrics;
    private final ObjectMapper json;
    private final HttpClient http=HttpClient.newBuilder().version(HttpClient.Version.HTTP_1_1).connectTimeout(Duration.ofSeconds(3)).build();

    public record Hit(ImageResponse image,double score) {}
    public record Result(String mode,String modelVersion,List<Hit> items,long tookMs) {}

    // Reused by BOTH retrieval branches and duplicate detection. Admin privileges are explicit.
    private String accessible(UserAccount actor,String scope,Long teamId,Map<String,Object> args) {
        if(!Set.of("public","accessible","mine","team").contains(scope)) throw new BadRequestException("Unknown search scope");
        args.put("actor",actor==null?-1L:actor.getId());
        args.put("admin",actor!=null && actor.getRole()==Role.ADMIN);
        args.put("team",teamId==null?-1L:teamId);
        String allowed="""
            a.deleted_at IS NULL AND a.processing_status='READY' AND
            ((a.visibility='PUBLIC' AND a.moderation_status='APPROVED') OR a.uploader_id=:actor OR :admin OR
             (a.visibility='TEAM' AND EXISTS(SELECT 1 FROM team_members m WHERE m.team_id=a.team_id AND m.user_id=:actor)))
            """;
        return allowed+switch(scope) {
            case "public" -> " AND a.visibility='PUBLIC' AND a.moderation_status='APPROVED'";
            case "mine" -> " AND a.uploader_id=:actor";
            case "team" -> " AND a.visibility='TEAM' AND a.team_id=:team";
            default -> "";
        };
    }

    public Result search(UserAccount actor,String query,String mode,String scope,Long teamId,int limit) {
        if(query==null || query.isBlank() || query.length()>300 || limit<1 || limit>50)
            throw new BadRequestException("Use a query of 1–300 characters and limit of 1–50");
        if(!Set.of("keyword","semantic","hybrid").contains(mode)) throw new BadRequestException("Unknown search mode");
        if(!mode.equals("keyword") && !properties.isSearchEnabled())
            throw new ResponseStatusException(HttpStatus.SERVICE_UNAVAILABLE,"Semantic search is not enabled");
        long start=System.nanoTime();
        try {
            var args=new HashMap<String,Object>(); args.put("q",query.trim()); args.put("limit",limit);
            args.put("model",properties.getModelVersion());
            String cte="WITH accessible AS MATERIALIZED (SELECT a.* FROM image_assets a WHERE "+accessible(actor,scope,teamId,args)+")";
            String lexical="""
                SELECT a.id,ts_rank_cd(to_tsvector('english',a.title||' '||a.description||' '||a.category||' '||a.tags),
                 websearch_to_tsquery('english',:q)) AS score FROM accessible a
                WHERE to_tsvector('english',a.title||' '||a.description||' '||a.category||' '||a.tags) @@ websearch_to_tsquery('english',:q)
                ORDER BY score DESC,a.id LIMIT 100
                """;
            String vector="""
                SELECT a.id,1-(e.embedding <=> CAST(:vector AS vector)) AS score
                FROM accessible a JOIN media_embeddings e ON e.media_id=a.id AND e.asset_version=a.asset_version
                WHERE e.model_version=:model ORDER BY e.embedding <=> CAST(:vector AS vector),a.id LIMIT 100
                """;
            if(!mode.equals("keyword")) args.put("vector",encode(query));
            String sql;
            if(mode.equals("hybrid")) {
                sql=cte+", lexical AS ("+lexical+"), semantic AS ("+vector+"), l AS (SELECT id,row_number() OVER(ORDER BY score DESC,id) r FROM lexical),"
                        +" s AS (SELECT id,row_number() OVER(ORDER BY score DESC,id) r FROM semantic) "
                        +"SELECT coalesce(l.id,s.id) id,coalesce(1.0/(60+l.r),0)+coalesce(1.0/(60+s.r),0) score "
                        +"FROM l FULL JOIN s ON l.id=s.id ORDER BY score DESC,id LIMIT :limit";
            } else sql=cte+", ranked AS ("+(mode.equals("keyword")?lexical:vector)+") SELECT * FROM ranked ORDER BY score DESC,id LIMIT :limit";
            var ranked=jdbc.query(sql,args,(r,n)->new long[]{r.getLong("id"),Double.doubleToLongBits(r.getDouble("score"))});
            var responses=images.accessibleResponses(ranked.stream().map(row -> row[0]).toList(),actor);
            var hits=new ArrayList<Hit>();
            for(int index=0;index<ranked.size();index++)
                hits.add(new Hit(responses.get(index),Double.longBitsToDouble(ranked.get(index)[1])));
            if(hits.isEmpty()) metrics.counter("semantic_search_no_result","mode",mode).increment();
            return new Result(mode,mode.equals("keyword")?null:properties.getModelVersion(),hits,(System.nanoTime()-start)/1_000_000);
        } finally { metrics.timer("semantic_search_duration","mode",mode).record(System.nanoTime()-start,java.util.concurrent.TimeUnit.NANOSECONDS); }
    }

    private String encode(String query) {
        try {
            var request=HttpRequest.newBuilder(URI.create(properties.getEncoderUrl()+"/encode"))
                    .timeout(Duration.ofSeconds(30)).header("Content-Type","application/json")
                    .header("Authorization","Bearer "+properties.getEncoderToken())
                    .POST(HttpRequest.BodyPublishers.ofString(json.writeValueAsString(Map.of("text",query,"modelVersion",properties.getModelVersion())))).build();
            var response=http.send(request,HttpResponse.BodyHandlers.ofString());
            if(response.statusCode()!=200) throw new IllegalStateException("Encoder unavailable");
            var body=json.readTree(response.body());
            if(!properties.getModelVersion().equals(body.path("modelVersion").asText())) throw new IllegalStateException("Encoder model mismatch");
            var values=body.path("embedding");
            if(!values.isArray() || values.size()!=512) throw new IllegalStateException("Invalid vector dimension");
            double norm=0;
            for(var value:values) {
                if(!value.isNumber() || !Double.isFinite(value.asDouble())) throw new IllegalStateException("Invalid embedding");
                norm+=value.asDouble()*value.asDouble();
            }
            if(Math.abs(norm-1)>0.02) throw new IllegalStateException("Embedding is not normalized");
            return values.toString();
        } catch(Exception exception) {
            if(exception instanceof InterruptedException) Thread.currentThread().interrupt();
            throw new ResponseStatusException(HttpStatus.SERVICE_UNAVAILABLE,"Search encoder unavailable; keyword search is still available");
        }
    }

    @Transactional(readOnly=true)
    public List<Map<String,Object>> duplicates(UserAccount actor,long id,int distance) {
        if(distance<0 || distance>16) throw new BadRequestException("Distance must be between 0 and 16");
        images.getAccessibleImage(id,actor);
        var args=new HashMap<String,Object>(); args.put("id",id); args.put("distance",distance);
        return jdbc.queryForList("""
            WITH source AS (SELECT content_sha256,perceptual_hash FROM image_assets WHERE id=:id),
            candidates AS (SELECT a.id,a.title,(a.content_sha256=s.content_sha256) AS exact,
             bit_count(('x'||a.perceptual_hash)::bit(64) # ('x'||s.perceptual_hash)::bit(64)) AS hamming_distance
             FROM image_assets a CROSS JOIN source s WHERE a.id<>:id AND
            """+accessible(actor,"accessible",null,args)+"""
            ) SELECT * FROM candidates WHERE exact OR hamming_distance<=:distance
            ORDER BY exact DESC,hamming_distance,id LIMIT 20
            """,args);
    }
}
