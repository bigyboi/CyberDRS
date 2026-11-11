/*
 * CyberDRS Vulkan probe layer.
 *
 * A tiny implicit Vulkan layer that watches colour-attachment render targets
 * created by the game and publishes the sizes it sees to
 * $HOME/.cache/cyberdrs/status.json. The Decky backend turns that into the
 * on-screen HUD.
 *
 * It is installed as an implicit layer with
 *   "enable_environment": { "SteamGameId": "1091500" }
 * so it only ever loads inside Cyberpunk 2077 and needs no launch options.
 *
 * Build:
 *   cc -shared -fPIC -O2 -Wall -Wextra -o libVkLayer_cyberdrs.so \
 *      cyberdrs_layer.c -lpthread
 *
 * This file is intentionally dependency-free beyond the Vulkan headers.
 */

#define _GNU_SOURCE
#define VK_NO_PROTOTYPES

#include <vulkan/vulkan.h>
#include <vulkan/vk_layer.h>

#include <math.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>
#include <limits.h>

#ifndef PATH_MAX
#define PATH_MAX 4096
#endif

#define CD_MAX_IMAGES 8192
#define CD_MAX_DEVICES 4
#define CD_MAX_CANDIDATES 64
#define CD_REPORT_CANDIDATES 12
#define CD_MIN_W 256
#define CD_MIN_H 144
#define CD_MIN_AREA (320ull * 180ull)

typedef struct {
    VkImage image;
    uint32_t w;
    uint32_t h;
    VkFormat format;
    double created;
    int used;
} CdImage;

typedef struct {
    VkDevice device;
    PFN_vkGetDeviceProcAddr next_gdpa;
    PFN_vkDestroyDevice DestroyDevice;
    PFN_vkCreateImage CreateImage;
    PFN_vkDestroyImage DestroyImage;
    PFN_vkCreateSwapchainKHR CreateSwapchainKHR;
    PFN_vkDestroySwapchainKHR DestroySwapchainKHR;
    CdImage images[CD_MAX_IMAGES];
    uint32_t out_w;
    uint32_t out_h;
    int used;
} CdDevice;

typedef struct {
    VkInstance instance;
    PFN_vkGetInstanceProcAddr next_gipa;
    PFN_vkDestroyInstance DestroyInstance;
    PFN_vkCreateDevice CreateDevice;
    int used;
} CdInstance;

typedef struct {
    uint32_t w;
    uint32_t h;
    uint32_t n;
    uint64_t area;
} CdCand;

static pthread_mutex_t g_lock = PTHREAD_MUTEX_INITIALIZER;
static CdDevice g_devices[CD_MAX_DEVICES];
static CdInstance g_instances[CD_MAX_DEVICES];
static pthread_t g_thread;
static int g_thread_started = 0;

/* Fallbacks so the layer can never break the game if a slot is unavailable
 * or the device was created through a path we did not see. */
static PFN_vkGetInstanceProcAddr g_last_gipa = NULL;
static PFN_vkGetDeviceProcAddr g_last_gdpa = NULL;

static double cd_wall(void) {
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
}

/* ------------------------------------------------------------------ state */

static CdInstance *cd_find_instance_locked(VkInstance instance) {
    for (int i = 0; i < CD_MAX_DEVICES; i++) {
        if (g_instances[i].used && g_instances[i].instance == instance)
            return &g_instances[i];
    }
    return NULL;
}

static CdInstance *cd_new_instance_locked(VkInstance instance) {
    for (int i = 0; i < CD_MAX_DEVICES; i++) {
        if (!g_instances[i].used) {
            memset(&g_instances[i], 0, sizeof(g_instances[i]));
            g_instances[i].used = 1;
            g_instances[i].instance = instance;
            return &g_instances[i];
        }
    }
    return NULL;
}

static CdDevice *cd_find_device_locked(VkDevice device) {
    for (int i = 0; i < CD_MAX_DEVICES; i++) {
        if (g_devices[i].used && g_devices[i].device == device)
            return &g_devices[i];
    }
    return NULL;
}

static CdDevice *cd_new_device_locked(VkDevice device) {
    for (int i = 0; i < CD_MAX_DEVICES; i++) {
        if (!g_devices[i].used) {
            memset(&g_devices[i], 0, sizeof(g_devices[i]));
            g_devices[i].used = 1;
            g_devices[i].device = device;
            return &g_devices[i];
        }
    }
    return NULL;
}

static int cd_is_color_format(VkFormat format) {
    switch (format) {
        case VK_FORMAT_UNDEFINED:
        case VK_FORMAT_D16_UNORM:
        case VK_FORMAT_X8_D24_UNORM_PACK32:
        case VK_FORMAT_D32_SFLOAT:
        case VK_FORMAT_S8_UINT:
        case VK_FORMAT_D16_UNORM_S8_UINT:
        case VK_FORMAT_D24_UNORM_S8_UINT:
        case VK_FORMAT_D32_SFLOAT_S8_UINT:
            return 0;
        default:
            return 1;
    }
}

static int cd_should_track(const VkImageCreateInfo *ci) {
    if (ci->imageType != VK_IMAGE_TYPE_2D)
        return 0;
    if (!(ci->usage & VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT))
        return 0;
    if (ci->samples != VK_SAMPLE_COUNT_1_BIT)
        return 0;
    if (ci->flags & VK_IMAGE_CREATE_PROTECTED_BIT)
        return 0;
    if (!cd_is_color_format(ci->format))
        return 0;
    if (ci->extent.width < CD_MIN_W || ci->extent.height < CD_MIN_H)
        return 0;
    if ((uint64_t)ci->extent.width * ci->extent.height < CD_MIN_AREA)
        return 0;
    return 1;
}

static void cd_register_image(CdDevice *dev, VkImage image, const VkImageCreateInfo *ci) {
    double now = cd_wall();
    pthread_mutex_lock(&g_lock);
    int slot = -1;
    int oldest = -1;
    double oldest_t = 1e300;
    for (int i = 0; i < CD_MAX_IMAGES; i++) {
        CdImage *rec = &dev->images[i];
        if (!rec->used) {
            slot = i;
            break;
        }
        if (rec->image == image) {
            slot = i;
            break;
        }
        if (rec->created < oldest_t) {
            oldest_t = rec->created;
            oldest = i;
        }
    }
    if (slot < 0)
        slot = oldest >= 0 ? oldest : 0;
    CdImage *rec = &dev->images[slot];
    rec->used = 1;
    rec->image = image;
    rec->w = ci->extent.width;
    rec->h = ci->extent.height;
    rec->format = ci->format;
    rec->created = now;
    pthread_mutex_unlock(&g_lock);
}

static void cd_unregister_image(CdDevice *dev, VkImage image) {
    pthread_mutex_lock(&g_lock);
    for (int i = 0; i < CD_MAX_IMAGES; i++) {
        if (dev->images[i].used && dev->images[i].image == image) {
            dev->images[i].used = 0;
            break;
        }
    }
    pthread_mutex_unlock(&g_lock);
}

/* ------------------------------------------------------------- reporting */

static int cd_add_candidate(CdCand *out, int count, uint32_t w, uint32_t h) {
    for (int i = 0; i < count; i++) {
        if (out[i].w == w && out[i].h == h) {
            out[i].n++;
            return count;
        }
    }
    if (count >= CD_MAX_CANDIDATES)
        return count;
    out[count].w = w;
    out[count].h = h;
    out[count].n = 1;
    out[count].area = (uint64_t)w * h;
    return count + 1;
}

static int cd_build_candidates(CdDevice *dev, CdCand *out, int max, int use_aspect) {
    int count = 0;
    double out_aspect = 0.0;
    uint64_t out_area = 0;
    if (dev->out_w && dev->out_h) {
        out_aspect = (double)dev->out_w / (double)dev->out_h;
        out_area = (uint64_t)dev->out_w * dev->out_h;
    }
    for (int i = 0; i < CD_MAX_IMAGES; i++) {
        CdImage *rec = &dev->images[i];
        if (!rec->used)
            continue;
        uint64_t area = (uint64_t)rec->w * rec->h;
        if (area < CD_MIN_AREA)
            continue;
        if (out_area && area > (uint64_t)(1.15 * (double)out_area))
            continue;
        if (use_aspect && out_aspect > 0.0) {
            double aspect = (double)rec->w / (double)rec->h;
            if (fabs(aspect - out_aspect) / out_aspect > 0.03)
                continue;
        }
        count = cd_add_candidate(out, count, rec->w, rec->h);
        if (count >= max)
            break;
    }
    /* Insertion sort, largest first. */
    for (int i = 1; i < count; i++) {
        CdCand key = out[i];
        int j = i - 1;
        while (j >= 0 && out[j].area < key.area) {
            out[j + 1] = out[j];
            j--;
        }
        out[j + 1] = key;
    }
    return count;
}

static int cd_pick_auto(const CdCand *cands, int count, uint32_t ow, uint32_t oh, CdCand *result) {
    if (count <= 0)
        return 0;
    *result = cands[0];
    if (!ow || !oh)
        return 1;
    uint64_t out_area = (uint64_t)ow * oh;
    int have_base = 0;
    for (int i = 0; i < count; i++) {
        if (cands[i].area >= (uint64_t)(0.95 * (double)out_area))
            have_base = 1;
    }
    if (!have_base)
        return 1;
    /* There is a target at (roughly) output size, so an upscaler is in play:
     * the largest target meaningfully below output size is the render target. */
    for (int i = 0; i < count; i++) {
        if (cands[i].area < (uint64_t)(0.95 * (double)out_area) &&
            cands[i].area >= (uint64_t)(0.20 * (double)out_area)) {
            *result = cands[i];
            return 1;
        }
    }
    return 1;
}

static void cd_write_status(CdDevice *dev) {
    CdCand cands[CD_MAX_CANDIDATES];
    CdCand chosen;
    int count = cd_build_candidates(dev, cands, CD_MAX_CANDIDATES, 1);
    if (count == 0)
        count = cd_build_candidates(dev, cands, CD_MAX_CANDIDATES, 0);
    if (count == 0)
        return;
    if (!cd_pick_auto(cands, count, dev->out_w, dev->out_h, &chosen))
        return;

    const char *home = getenv("HOME");
    if (!home || !*home)
        return;

    char dir[PATH_MAX];
    char path[PATH_MAX];
    char tmp[PATH_MAX];
    snprintf(dir, sizeof(dir), "%s/.cache", home);
    mkdir(dir, 0777);
    snprintf(dir, sizeof(dir), "%s/.cache/cyberdrs", home);
    mkdir(dir, 0777);
    snprintf(path, sizeof(path), "%s/status.json", dir);
    snprintf(tmp, sizeof(tmp), "%s/status.json.tmp.%d", dir, (int)getpid());

    FILE *fh = fopen(tmp, "w");
    if (!fh)
        return;

    fprintf(fh,
            "{\"ts\":%.3f,\"pid\":%d,\"output\":[%u,%u],"
            "\"auto\":{\"w\":%u,\"h\":%u},\"candidates\":[",
            cd_wall(), (int)getpid(), dev->out_w, dev->out_h, chosen.w, chosen.h);
    for (int i = 0; i < count && i < CD_REPORT_CANDIDATES; i++) {
        fprintf(fh, "%s{\"w\":%u,\"h\":%u,\"n\":%u}", i ? "," : "", cands[i].w, cands[i].h, cands[i].n);
    }
    fprintf(fh, "]}\n");
    fclose(fh);
    rename(tmp, path);
}

static void *cd_refresher(void *arg) {
    (void)arg;
    for (;;) {
        struct timespec ts;
        ts.tv_sec = 0;
        ts.tv_nsec = 500 * 1000 * 1000;
        nanosleep(&ts, NULL);
        pthread_mutex_lock(&g_lock);
        for (int i = 0; i < CD_MAX_DEVICES; i++) {
            if (g_devices[i].used)
                cd_write_status(&g_devices[i]);
        }
        pthread_mutex_unlock(&g_lock);
    }
    return NULL;
}

static void cd_ensure_thread(void) {
    if (g_thread_started)
        return;
    g_thread_started = 1;
    if (pthread_create(&g_thread, NULL, cd_refresher, NULL) == 0)
        pthread_detach(g_thread);
}

/* ------------------------------------------------------------ trampolines */

static VKAPI_ATTR VkResult VKAPI_CALL cd_CreateInstance(const VkInstanceCreateInfo *ci,
                                                        const VkAllocationCallbacks *alloc,
                                                        VkInstance *pInstance) {
    VkLayerInstanceCreateInfo *link = NULL;
    const VkBaseInStructure *hdr = (const VkBaseInStructure *)ci->pNext;
    while (hdr) {
        if (hdr->sType == VK_STRUCTURE_TYPE_LOADER_INSTANCE_CREATE_INFO) {
            VkLayerInstanceCreateInfo *cand = (VkLayerInstanceCreateInfo *)hdr;
            if (cand->function == VK_LAYER_LINK_INFO) {
                link = cand;
                break;
            }
        }
        hdr = hdr->pNext;
    }
    if (!link || !link->u.pLayerInfo)
        return VK_ERROR_INITIALIZATION_FAILED;

    PFN_vkGetInstanceProcAddr next_gipa = link->u.pLayerInfo->pfnNextGetInstanceProcAddr;
    PFN_vkCreateInstance next_create =
        (PFN_vkCreateInstance)next_gipa(NULL, "vkCreateInstance");
    link->u.pLayerInfo = link->u.pLayerInfo->pNext;
    if (!next_create)
        return VK_ERROR_INITIALIZATION_FAILED;

    VkResult result = next_create(ci, alloc, pInstance);
    if (result != VK_SUCCESS)
        return result;

    pthread_mutex_lock(&g_lock);
    g_last_gipa = next_gipa;
    CdInstance *inst = cd_new_instance_locked(*pInstance);
    if (inst) {
        inst->next_gipa = next_gipa;
        inst->DestroyInstance = (PFN_vkDestroyInstance)next_gipa(*pInstance, "vkDestroyInstance");
        inst->CreateDevice = (PFN_vkCreateDevice)next_gipa(*pInstance, "vkCreateDevice");
    }
    pthread_mutex_unlock(&g_lock);
    return result;
}

static VKAPI_ATTR void VKAPI_CALL cd_DestroyInstance(VkInstance instance,
                                                     const VkAllocationCallbacks *alloc) {
    pthread_mutex_lock(&g_lock);
    CdInstance *inst = cd_find_instance_locked(instance);
    PFN_vkDestroyInstance next = inst ? inst->DestroyInstance : NULL;
    if (inst)
        inst->used = 0;
    pthread_mutex_unlock(&g_lock);
    if (next)
        next(instance, alloc);
}

static VKAPI_ATTR VkResult VKAPI_CALL cd_CreateDevice(VkPhysicalDevice gpu,
                                                      const VkDeviceCreateInfo *ci,
                                                      const VkAllocationCallbacks *alloc,
                                                      VkDevice *pDevice) {
    VkLayerDeviceCreateInfo *link = NULL;
    const VkBaseInStructure *hdr = (const VkBaseInStructure *)ci->pNext;
    while (hdr) {
        if (hdr->sType == VK_STRUCTURE_TYPE_LOADER_DEVICE_CREATE_INFO) {
            VkLayerDeviceCreateInfo *cand = (VkLayerDeviceCreateInfo *)hdr;
            if (cand->function == VK_LAYER_LINK_INFO) {
                link = cand;
                break;
            }
        }
        hdr = hdr->pNext;
    }
    if (!link || !link->u.pLayerInfo)
        return VK_ERROR_INITIALIZATION_FAILED;

    PFN_vkGetInstanceProcAddr next_gipa = link->u.pLayerInfo->pfnNextGetInstanceProcAddr;
    PFN_vkGetDeviceProcAddr next_gdpa_link = link->u.pLayerInfo->pfnNextGetDeviceProcAddr;
    PFN_vkCreateDevice next_create = (PFN_vkCreateDevice)next_gipa(NULL, "vkCreateDevice");
    link->u.pLayerInfo = link->u.pLayerInfo->pNext;
    if (!next_create)
        return VK_ERROR_INITIALIZATION_FAILED;

    VkResult result = next_create(gpu, ci, alloc, pDevice);
    if (result != VK_SUCCESS)
        return result;

    pthread_mutex_lock(&g_lock);
    CdDevice *dev = cd_new_device_locked(*pDevice);
    if (dev) {
        dev->next_gdpa = (PFN_vkGetDeviceProcAddr)next_gdpa_link(*pDevice, "vkGetDeviceProcAddr");
        g_last_gdpa = dev->next_gdpa;
        if (dev->next_gdpa) {
            dev->DestroyDevice = (PFN_vkDestroyDevice)dev->next_gdpa(*pDevice, "vkDestroyDevice");
            dev->CreateImage = (PFN_vkCreateImage)dev->next_gdpa(*pDevice, "vkCreateImage");
            dev->DestroyImage = (PFN_vkDestroyImage)dev->next_gdpa(*pDevice, "vkDestroyImage");
            dev->CreateSwapchainKHR =
                (PFN_vkCreateSwapchainKHR)dev->next_gdpa(*pDevice, "vkCreateSwapchainKHR");
            dev->DestroySwapchainKHR =
                (PFN_vkDestroySwapchainKHR)dev->next_gdpa(*pDevice, "vkDestroySwapchainKHR");
        }
    }
    cd_ensure_thread();
    pthread_mutex_unlock(&g_lock);
    return result;
}

static VKAPI_ATTR void VKAPI_CALL cd_DestroyDevice(VkDevice device,
                                                   const VkAllocationCallbacks *alloc) {
    pthread_mutex_lock(&g_lock);
    CdDevice *dev = cd_find_device_locked(device);
    PFN_vkDestroyDevice next = dev ? dev->DestroyDevice : NULL;
    if (dev)
        dev->used = 0;
    pthread_mutex_unlock(&g_lock);
    if (!next) {
        PFN_vkGetDeviceProcAddr gdpa = g_last_gdpa;
        next = gdpa ? (PFN_vkDestroyDevice)gdpa(device, "vkDestroyDevice") : NULL;
    }
    if (next)
        next(device, alloc);
}

static PFN_vkVoidFunction cd_next_dev_fn(VkDevice device, const char *name) {
    pthread_mutex_lock(&g_lock);
    CdDevice *dev = cd_find_device_locked(device);
    PFN_vkGetDeviceProcAddr gdpa = dev ? dev->next_gdpa : g_last_gdpa;
    pthread_mutex_unlock(&g_lock);
    return gdpa ? gdpa(device, name) : NULL;
}

static VKAPI_ATTR VkResult VKAPI_CALL cd_CreateImage(VkDevice device,
                                                     const VkImageCreateInfo *ci,
                                                     const VkAllocationCallbacks *alloc,
                                                     VkImage *pImage) {
    pthread_mutex_lock(&g_lock);
    CdDevice *dev = cd_find_device_locked(device);
    PFN_vkCreateImage next = dev ? dev->CreateImage : NULL;
    pthread_mutex_unlock(&g_lock);
    if (!next)
        next = (PFN_vkCreateImage)cd_next_dev_fn(device, "vkCreateImage");
    if (!next)
        return VK_ERROR_INITIALIZATION_FAILED;

    VkResult result = next(device, ci, alloc, pImage);
    if (result == VK_SUCCESS && ci && pImage && *pImage && dev && cd_should_track(ci))
        cd_register_image(dev, *pImage, ci);
    return result;
}

static VKAPI_ATTR void VKAPI_CALL cd_DestroyImage(VkDevice device,
                                                  VkImage image,
                                                  const VkAllocationCallbacks *alloc) {
    pthread_mutex_lock(&g_lock);
    CdDevice *dev = cd_find_device_locked(device);
    PFN_vkDestroyImage next = dev ? dev->DestroyImage : NULL;
    pthread_mutex_unlock(&g_lock);
    if (!next)
        next = (PFN_vkDestroyImage)cd_next_dev_fn(device, "vkDestroyImage");
    if (dev)
        cd_unregister_image(dev, image);
    if (next)
        next(device, image, alloc);
}

static VKAPI_ATTR VkResult VKAPI_CALL cd_CreateSwapchainKHR(VkDevice device,
                                                            const VkSwapchainCreateInfoKHR *ci,
                                                            const VkAllocationCallbacks *alloc,
                                                            VkSwapchainKHR *pSwapchain) {
    pthread_mutex_lock(&g_lock);
    CdDevice *dev = cd_find_device_locked(device);
    PFN_vkCreateSwapchainKHR next = dev ? dev->CreateSwapchainKHR : NULL;
    pthread_mutex_unlock(&g_lock);
    if (!next)
        next = (PFN_vkCreateSwapchainKHR)cd_next_dev_fn(device, "vkCreateSwapchainKHR");
    if (!next)
        return VK_ERROR_INITIALIZATION_FAILED;

    VkResult result = next(device, ci, alloc, pSwapchain);
    if (result == VK_SUCCESS && ci && ci->imageExtent.width && ci->imageExtent.height) {
        pthread_mutex_lock(&g_lock);
        if (dev) {
            dev->out_w = ci->imageExtent.width;
            dev->out_h = ci->imageExtent.height;
        }
        pthread_mutex_unlock(&g_lock);
    }
    return result;
}

static VKAPI_ATTR void VKAPI_CALL cd_DestroySwapchainKHR(VkDevice device,
                                                         VkSwapchainKHR swapchain,
                                                         const VkAllocationCallbacks *alloc) {
    pthread_mutex_lock(&g_lock);
    CdDevice *dev = cd_find_device_locked(device);
    PFN_vkDestroySwapchainKHR next = dev ? dev->DestroySwapchainKHR : NULL;
    pthread_mutex_unlock(&g_lock);
    if (!next)
        next = (PFN_vkDestroySwapchainKHR)cd_next_dev_fn(device, "vkDestroySwapchainKHR");
    if (next)
        next(device, swapchain, alloc);
}

/* --------------------------------------------------------------- dispatch */

static VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL cd_GetDeviceProcAddr(VkDevice device,
                                                                    const char *name);

static VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL cd_GetInstanceProcAddr(VkInstance instance,
                                                                      const char *name) {
    if (!name)
        return NULL;
    if (strcmp(name, "vkGetInstanceProcAddr") == 0)
        return (PFN_vkVoidFunction)cd_GetInstanceProcAddr;
    if (strcmp(name, "vkGetDeviceProcAddr") == 0)
        return (PFN_vkVoidFunction)cd_GetDeviceProcAddr;
    if (strcmp(name, "vkCreateInstance") == 0)
        return (PFN_vkVoidFunction)cd_CreateInstance;
    if (strcmp(name, "vkDestroyInstance") == 0)
        return (PFN_vkVoidFunction)cd_DestroyInstance;
    if (strcmp(name, "vkCreateDevice") == 0)
        return (PFN_vkVoidFunction)cd_CreateDevice;
    if (strcmp(name, "vkDestroyDevice") == 0)
        return (PFN_vkVoidFunction)cd_DestroyDevice;
    if (strcmp(name, "vkCreateImage") == 0)
        return (PFN_vkVoidFunction)cd_CreateImage;
    if (strcmp(name, "vkDestroyImage") == 0)
        return (PFN_vkVoidFunction)cd_DestroyImage;
    if (strcmp(name, "vkCreateSwapchainKHR") == 0)
        return (PFN_vkVoidFunction)cd_CreateSwapchainKHR;
    if (strcmp(name, "vkDestroySwapchainKHR") == 0)
        return (PFN_vkVoidFunction)cd_DestroySwapchainKHR;

    pthread_mutex_lock(&g_lock);
    CdInstance *inst = instance ? cd_find_instance_locked(instance) : NULL;
    PFN_vkGetInstanceProcAddr next = inst ? inst->next_gipa : NULL;
    pthread_mutex_unlock(&g_lock);
    if (!next)
        next = g_last_gipa;
    if (!next)
        return NULL;
    return next(instance, name);
}

static VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL cd_GetDeviceProcAddr(VkDevice device,
                                                                    const char *name) {
    if (!name)
        return NULL;
    if (strcmp(name, "vkGetDeviceProcAddr") == 0)
        return (PFN_vkVoidFunction)cd_GetDeviceProcAddr;
    if (strcmp(name, "vkCreateImage") == 0)
        return (PFN_vkVoidFunction)cd_CreateImage;
    if (strcmp(name, "vkDestroyImage") == 0)
        return (PFN_vkVoidFunction)cd_DestroyImage;
    if (strcmp(name, "vkCreateSwapchainKHR") == 0)
        return (PFN_vkVoidFunction)cd_CreateSwapchainKHR;
    if (strcmp(name, "vkDestroySwapchainKHR") == 0)
        return (PFN_vkVoidFunction)cd_DestroySwapchainKHR;
    if (strcmp(name, "vkDestroyDevice") == 0)
        return (PFN_vkVoidFunction)cd_DestroyDevice;

    pthread_mutex_lock(&g_lock);
    CdDevice *dev = device ? cd_find_device_locked(device) : NULL;
    PFN_vkGetDeviceProcAddr next = dev ? dev->next_gdpa : NULL;
    pthread_mutex_unlock(&g_lock);
    if (!next)
        next = g_last_gdpa;
    if (!next)
        return NULL;
    return next(device, name);
}

/* ---------------------------------------------------------------- exports */

__attribute__((visibility("default"))) VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL
vkGetInstanceProcAddr(VkInstance instance, const char *name) {
    return cd_GetInstanceProcAddr(instance, name);
}

__attribute__((visibility("default"))) VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL
vkGetDeviceProcAddr(VkDevice device, const char *name) {
    return cd_GetDeviceProcAddr(device, name);
}

__attribute__((visibility("default"))) VKAPI_ATTR VkResult VKAPI_CALL
vkNegotiateLoaderLayerInterfaceVersion(VkNegotiateLayerInterface *iface) {
    if (!iface || iface->sType != LAYER_NEGOTIATE_INTERFACE_STRUCT)
        return VK_ERROR_INITIALIZATION_FAILED;
    if (iface->loaderLayerInterfaceVersion > 2)
        iface->loaderLayerInterfaceVersion = 2;
    iface->pfnGetInstanceProcAddr = cd_GetInstanceProcAddr;
    iface->pfnGetDeviceProcAddr = cd_GetDeviceProcAddr;
    iface->pfnGetPhysicalDeviceProcAddr = NULL;
    return VK_SUCCESS;
}
