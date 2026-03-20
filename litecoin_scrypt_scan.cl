// litecoin_scrypt_scan.cl
#pragma OPENCL EXTENSION cl_khr_byte_addressable_store : enable

#define LTC_HASH_BYTES              32u
#define LTC_HEADER76_BYTES          76u
#define LTC_HEADER80_BYTES          80u

#define SCRYPT_N                    1024u
#define SCRYPT_R                    1u
#define SCRYPT_P                    1u
#define SCRYPT_BLOCK_BYTES          (128u * SCRYPT_R)              // 128
#define SCRYPT_BLOCK_WORDS          (SCRYPT_BLOCK_BYTES / 4u)      // 32
#define SCRYPT_V_WORDS              (SCRYPT_N * SCRYPT_BLOCK_WORDS) // 32768

#define PBKDF2_MAX_SALT_BYTES       128u
#define PBKDF2_MAX_MSG_BYTES        (PBKDF2_MAX_SALT_BYTES + 4u)

#ifndef LTC_HASHES_PER_THREAD
#define LTC_HASHES_PER_THREAD       1u
#endif

inline uint rotl32(uint x, uint n) { return (x << n) | (x >> (32u - n)); }
inline uint rotr32(uint x, uint n) { return (x >> n) | (x << (32u - n)); }

inline uint load_be32(const uchar* p)
{
    return ((uint)p[0] << 24) | ((uint)p[1] << 16) | ((uint)p[2] << 8) | (uint)p[3];
}

inline uint load_le32(const uchar* p)
{
    return ((uint)p[0]) | ((uint)p[1] << 8) | ((uint)p[2] << 16) | ((uint)p[3] << 24);
}

inline void store_be32(uchar* p, uint x)
{
    p[0] = (uchar)((x >> 24) & 0xffu);
    p[1] = (uchar)((x >> 16) & 0xffu);
    p[2] = (uchar)((x >> 8) & 0xffu);
    p[3] = (uchar)(x & 0xffu);
}

inline void store_le32(uchar* p, uint x)
{
    p[0] = (uchar)(x & 0xffu);
    p[1] = (uchar)((x >> 8) & 0xffu);
    p[2] = (uchar)((x >> 16) & 0xffu);
    p[3] = (uchar)((x >> 24) & 0xffu);
}

__constant uint K256[64] = {
    0x428a2f98u, 0x71374491u, 0xb5c0fbcfu, 0xe9b5dba5u,
    0x3956c25bu, 0x59f111f1u, 0x923f82a4u, 0xab1c5ed5u,
    0xd807aa98u, 0x12835b01u, 0x243185beu, 0x550c7dc3u,
    0x72be5d74u, 0x80deb1feu, 0x9bdc06a7u, 0xc19bf174u,
    0xe49b69c1u, 0xefbe4786u, 0x0fc19dc6u, 0x240ca1ccu,
    0x2de92c6fu, 0x4a7484aau, 0x5cb0a9dcu, 0x76f988dau,
    0x983e5152u, 0xa831c66du, 0xb00327c8u, 0xbf597fc7u,
    0xc6e00bf3u, 0xd5a79147u, 0x06ca6351u, 0x14292967u,
    0x27b70a85u, 0x2e1b2138u, 0x4d2c6dfcu, 0x53380d13u,
    0x650a7354u, 0x766a0abbu, 0x81c2c92eu, 0x92722c85u,
    0xa2bfe8a1u, 0xa81a664bu, 0xc24b8b70u, 0xc76c51a3u,
    0xd192e819u, 0xd6990624u, 0xf40e3585u, 0x106aa070u,
    0x19a4c116u, 0x1e376c08u, 0x2748774cu, 0x34b0bcb5u,
    0x391c0cb3u, 0x4ed8aa4au, 0x5b9cca4fu, 0x682e6ff3u,
    0x748f82eeu, 0x78a5636fu, 0x84c87814u, 0x8cc70208u,
    0x90befffau, 0xa4506cebu, 0xbef9a3f7u, 0xc67178f2u
};

typedef struct
{
    uint h[8];
    ulong total_len;
    uint buf_len;
    uchar buf[64];
} sha256_ctx;

inline void sha256_ctx_copy(sha256_ctx* dst, const sha256_ctx* src)
{
    for (int i = 0; i < 8; ++i) dst->h[i] = src->h[i];
    dst->total_len = src->total_len;
    dst->buf_len = src->buf_len;
    for (int i = 0; i < 64; ++i) dst->buf[i] = src->buf[i];
}

inline void sha256_init(sha256_ctx* ctx)
{
    ctx->h[0] = 0x6a09e667u;
    ctx->h[1] = 0xbb67ae85u;
    ctx->h[2] = 0x3c6ef372u;
    ctx->h[3] = 0xa54ff53au;
    ctx->h[4] = 0x510e527fu;
    ctx->h[5] = 0x9b05688cu;
    ctx->h[6] = 0x1f83d9abu;
    ctx->h[7] = 0x5be0cd19u;
    ctx->total_len = 0ul;
    ctx->buf_len = 0u;
    for (int i = 0; i < 64; ++i) ctx->buf[i] = 0u;
}

inline void sha256_transform(sha256_ctx* ctx, const uchar block[64])
{
    uint w[64];

    for (int i = 0; i < 16; ++i)
        w[i] = load_be32(block + (i * 4));

    for (int i = 16; i < 64; ++i)
    {
        uint s0 = rotr32(w[i - 15], 7u) ^ rotr32(w[i - 15], 18u) ^ (w[i - 15] >> 3);
        uint s1 = rotr32(w[i - 2], 17u) ^ rotr32(w[i - 2], 19u) ^ (w[i - 2] >> 10);
        w[i] = w[i - 16] + s0 + w[i - 7] + s1;
    }

    uint a = ctx->h[0];
    uint b = ctx->h[1];
    uint c = ctx->h[2];
    uint d = ctx->h[3];
    uint e = ctx->h[4];
    uint f = ctx->h[5];
    uint g = ctx->h[6];
    uint h = ctx->h[7];

    for (int i = 0; i < 64; ++i)
    {
        uint S1 = rotr32(e, 6u) ^ rotr32(e, 11u) ^ rotr32(e, 25u);
        uint ch = (e & f) ^ ((~e) & g);
        uint temp1 = h + S1 + ch + K256[i] + w[i];
        uint S0 = rotr32(a, 2u) ^ rotr32(a, 13u) ^ rotr32(a, 22u);
        uint maj = (a & b) ^ (a & c) ^ (b & c);
        uint temp2 = S0 + maj;

        h = g;
        g = f;
        f = e;
        e = d + temp1;
        d = c;
        c = b;
        b = a;
        a = temp1 + temp2;
    }

    ctx->h[0] += a;
    ctx->h[1] += b;
    ctx->h[2] += c;
    ctx->h[3] += d;
    ctx->h[4] += e;
    ctx->h[5] += f;
    ctx->h[6] += g;
    ctx->h[7] += h;
}

inline void sha256_update(sha256_ctx* ctx, const uchar* data, uint len)
{
    ctx->total_len += (ulong)len;

    uint offset = 0u;

    if (ctx->buf_len != 0u)
    {
        uint take = 64u - ctx->buf_len;
        if (take > len) take = len;

        for (uint i = 0u; i < take; ++i)
            ctx->buf[ctx->buf_len + i] = data[i];

        ctx->buf_len += take;
        offset += take;

        if (ctx->buf_len == 64u)
        {
            sha256_transform(ctx, ctx->buf);
            ctx->buf_len = 0u;
        }
    }

    while ((len - offset) >= 64u)
    {
        sha256_transform(ctx, data + offset);
        offset += 64u;
    }

    for (uint i = 0u; i < (len - offset); ++i)
        ctx->buf[i] = data[offset + i];

    ctx->buf_len = len - offset;
}

inline void sha256_final(sha256_ctx* ctx, uchar out32[32])
{
    ulong bit_len = ctx->total_len * 8ul;

    ctx->buf[ctx->buf_len++] = 0x80u;

    if (ctx->buf_len > 56u)
    {
        while (ctx->buf_len < 64u)
            ctx->buf[ctx->buf_len++] = 0x00u;
        sha256_transform(ctx, ctx->buf);
        ctx->buf_len = 0u;
    }

    while (ctx->buf_len < 56u)
        ctx->buf[ctx->buf_len++] = 0x00u;

    ctx->buf[56] = (uchar)((bit_len >> 56) & 0xfful);
    ctx->buf[57] = (uchar)((bit_len >> 48) & 0xfful);
    ctx->buf[58] = (uchar)((bit_len >> 40) & 0xfful);
    ctx->buf[59] = (uchar)((bit_len >> 32) & 0xfful);
    ctx->buf[60] = (uchar)((bit_len >> 24) & 0xfful);
    ctx->buf[61] = (uchar)((bit_len >> 16) & 0xfful);
    ctx->buf[62] = (uchar)((bit_len >> 8) & 0xfful);
    ctx->buf[63] = (uchar)(bit_len & 0xfful);

    sha256_transform(ctx, ctx->buf);
    ctx->buf_len = 0u;

    for (int i = 0; i < 8; ++i)
        store_be32(out32 + (i * 4), ctx->h[i]);
}

inline void sha256_bytes(const uchar* data, uint len, uchar out32[32])
{
    sha256_ctx ctx;
    sha256_init(&ctx);
    sha256_update(&ctx, data, len);
    sha256_final(&ctx, out32);
}

inline void hmac_sha256_key_setup(
    const uchar* key,
    uint key_len,
    sha256_ctx* ipad_base,
    sha256_ctx* opad_base
)
{
    uchar key_block[64];
    uchar key_hash[32];

    for (int i = 0; i < 64; ++i) key_block[i] = 0u;

    if (key_len > 64u)
    {
        sha256_bytes(key, key_len, key_hash);
        for (int i = 0; i < 32; ++i) key_block[i] = key_hash[i];
    }
    else
    {
        for (uint i = 0u; i < key_len; ++i) key_block[i] = key[i];
    }

    uchar ipad[64];
    uchar opad[64];

    for (int i = 0; i < 64; ++i)
    {
        ipad[i] = (uchar)(key_block[i] ^ 0x36u);
        opad[i] = (uchar)(key_block[i] ^ 0x5cu);
    }

    sha256_init(ipad_base);
    sha256_update(ipad_base, ipad, 64u);

    sha256_init(opad_base);
    sha256_update(opad_base, opad, 64u);
}

inline void hmac_sha256_from_bases(
    const sha256_ctx* ipad_base,
    const sha256_ctx* opad_base,
    const uchar* msg,
    uint msg_len,
    uchar out32[32]
)
{
    sha256_ctx ictx;
    sha256_ctx octx;
    uchar inner[32];

    sha256_ctx_copy(&ictx, ipad_base);
    sha256_update(&ictx, msg, msg_len);
    sha256_final(&ictx, inner);

    sha256_ctx_copy(&octx, opad_base);
    sha256_update(&octx, inner, 32u);
    sha256_final(&octx, out32);
}

inline void pbkdf2_hmac_sha256_c1(
    const sha256_ctx* ipad_base,
    const sha256_ctx* opad_base,
    const uchar* salt,
    uint salt_len,
    uint blocks,
    uchar* out
)
{
    uchar msg[PBKDF2_MAX_MSG_BYTES];

    for (uint block = 1u; block <= blocks; ++block)
    {
        for (uint i = 0u; i < salt_len; ++i)
            msg[i] = salt[i];

        msg[salt_len + 0u] = (uchar)((block >> 24) & 0xffu);
        msg[salt_len + 1u] = (uchar)((block >> 16) & 0xffu);
        msg[salt_len + 2u] = (uchar)((block >> 8) & 0xffu);
        msg[salt_len + 3u] = (uchar)(block & 0xffu);

        hmac_sha256_from_bases(
            ipad_base,
            opad_base,
            msg,
            salt_len + 4u,
            out + ((block - 1u) * 32u)
        );
    }
}

#define QR(a,b,c,d) \
    b ^= rotl32((a + d), 7u); \
    c ^= rotl32((b + a), 9u); \
    d ^= rotl32((c + b), 13u); \
    a ^= rotl32((d + c), 18u);

inline void salsa20_8(uint B[16])
{
    uint x[16];
    for (int i = 0; i < 16; ++i) x[i] = B[i];

    for (int i = 0; i < 8; i += 2)
    {
        QR(x[0],  x[4],  x[8],  x[12]);
        QR(x[5],  x[9],  x[13], x[1]);
        QR(x[10], x[14], x[2],  x[6]);
        QR(x[15], x[3],  x[7],  x[11]);

        QR(x[0],  x[1],  x[2],  x[3]);
        QR(x[5],  x[6],  x[7],  x[4]);
        QR(x[10], x[11], x[8],  x[9]);
        QR(x[15], x[12], x[13], x[14]);
    }

    for (int i = 0; i < 16; ++i)
        B[i] += x[i];
}

inline void blockmix_salsa8_r1(const uint inB[SCRYPT_BLOCK_WORDS], uint outB[SCRYPT_BLOCK_WORDS])
{
    uint X[16];
    uint T[16];

    for (int i = 0; i < 16; ++i)
        X[i] = inB[16 + i];

    for (int i = 0; i < 16; ++i)
        T[i] = X[i] ^ inB[i];
    salsa20_8(T);
    for (int i = 0; i < 16; ++i)
    {
        outB[i] = T[i];
        X[i] = T[i];
    }

    for (int i = 0; i < 16; ++i)
        T[i] = X[i] ^ inB[16 + i];
    salsa20_8(T);
    for (int i = 0; i < 16; ++i)
        outB[16 + i] = T[i];
}

inline uint integerify_mod_n_r1(const uint B[SCRYPT_BLOCK_WORDS])
{
    return B[16] & (SCRYPT_N - 1u);
}

inline void scrypt_1024_1_1_hash_le(
    const uchar header80[80],
    __global uint* scratch_v_words,
    uchar out32_le[32]
)
{
    sha256_ctx ipad_base;
    sha256_ctx opad_base;

    uchar B0_bytes[128];
    uchar Bf_bytes[128];
    uchar digest_be[32];

    uint X[SCRYPT_BLOCK_WORDS];
    uint T[SCRYPT_BLOCK_WORDS];
    uint Y[SCRYPT_BLOCK_WORDS];

    hmac_sha256_key_setup(header80, 80u, &ipad_base, &opad_base);

    // PBKDF2-HMAC-SHA256(P=header80, S=header80, c=1, dkLen=128)
    pbkdf2_hmac_sha256_c1(&ipad_base, &opad_base, header80, 80u, 4u, B0_bytes);

    for (int i = 0; i < (int)SCRYPT_BLOCK_WORDS; ++i)
        X[i] = load_le32(B0_bytes + (i * 4));

    for (uint i = 0u; i < SCRYPT_N; ++i)
    {
        uint base = i * SCRYPT_BLOCK_WORDS;
        for (uint k = 0u; k < SCRYPT_BLOCK_WORDS; ++k)
            scratch_v_words[base + k] = X[k];

        blockmix_salsa8_r1(X, Y);

        for (uint k = 0u; k < SCRYPT_BLOCK_WORDS; ++k)
            X[k] = Y[k];
    }

    for (uint i = 0u; i < SCRYPT_N; ++i)
    {
        uint j = integerify_mod_n_r1(X);
        uint base = j * SCRYPT_BLOCK_WORDS;

        for (uint k = 0u; k < SCRYPT_BLOCK_WORDS; ++k)
            T[k] = X[k] ^ scratch_v_words[base + k];

        blockmix_salsa8_r1(T, Y);

        for (uint k = 0u; k < SCRYPT_BLOCK_WORDS; ++k)
            X[k] = Y[k];
    }

    for (int i = 0; i < (int)SCRYPT_BLOCK_WORDS; ++i)
        store_le32(Bf_bytes + (i * 4), X[i]);

    // PBKDF2-HMAC-SHA256(P=header80, S=Bf, c=1, dkLen=32)
    pbkdf2_hmac_sha256_c1(&ipad_base, &opad_base, Bf_bytes, 128u, 1u, digest_be);

    for (int i = 0; i < 32; ++i)
        out32_le[i] = digest_be[31 - i];
}

inline int hash_meets_target_le32(
    const uchar hash32_le[32],
    __global const uchar* target32_le
)
{
    for (int i = 31; i >= 0; --i)
    {
        uchar h = hash32_le[i];
        uchar t = target32_le[i];
        if (h < t) return 1;
        if (h > t) return 0;
    }
    return 1;
}

__kernel void ltc_scrypt_scan(
    __global const uchar* header76,
    __global const uchar* target32_le,
    const uint start_nonce,
    const uint count,
    const uint max_results,
    __global uint* scratch_words,
    __global uint* out_count,
    __global uint* out_nonces,
    __global uchar* out_hashes
)
{
    const uint gid = (uint)get_global_id(0);
    const uint gsz = (uint)get_global_size(0);

    if (gid >= count && LTC_HASHES_PER_THREAD == 1u)
        return;

    uchar header80[80];
    for (int i = 0; i < 76; ++i)
        header80[i] = header76[i];

    __global uint* my_scratch = scratch_words + ((size_t)gid * (size_t)SCRYPT_V_WORDS);

    for (uint iter = 0u; iter < LTC_HASHES_PER_THREAD; ++iter)
    {
        uint logical_index = gid + iter * gsz;
        if (logical_index >= count)
            break;

        uint nonce = start_nonce + logical_index;

        header80[76] = (uchar)( nonce        & 0xffu);
        header80[77] = (uchar)((nonce >> 8)  & 0xffu);
        header80[78] = (uchar)((nonce >> 16) & 0xffu);
        header80[79] = (uchar)((nonce >> 24) & 0xffu);

        uchar hash32_le[32];
        scrypt_1024_1_1_hash_le(header80, my_scratch, hash32_le);

        if (!hash_meets_target_le32(hash32_le, target32_le))
            continue;

        uint slot = atomic_inc((volatile __global uint*)out_count);
        if (slot >= max_results)
            continue;

        out_nonces[slot] = nonce;

        uint hash_base = slot * LTC_HASH_BYTES;
        for (int i = 0; i < 32; ++i)
            out_hashes[hash_base + (uint)i] = hash32_le[i];
    }
}