#include "types.h"

struct QueryPointPair { f32 y; f32 x; };
extern s32 func_8010BC10_us(struct QueryPointPair, struct QueryPointPair,
                          struct QueryPointPair, struct QueryPointPair,
                          struct QueryPointPair *);

/* Bounds are four measured doubles: min-x, max-x, max-y, min-y.
 * Quad vertices are four measured f32 pairs. Return 1 is a corner response,
 * 2 is an axis response, and 0 leaves caller outputs unchanged. */
s32 func_8010AC90_us(void *bounds_arg, void *quad_arg, void *delta_arg, void *contact_arg, void *normal_arg)
{
  f64 *bounds = (f64 *) bounds_arg;
  struct QueryPointPair *quad = (struct QueryPointPair *) quad_arg;
  struct QueryPointPair *delta = (struct QueryPointPair *) delta_arg;
  struct QueryPointPair *contact = (struct QueryPointPair *) contact_arg;
  struct QueryPointPair *normal = (struct QueryPointPair *) normal_arg;
  struct QueryPointPair start;
  struct QueryPointPair end;
  struct QueryPointPair hit;
  f64 cross;
  f32 normal_x;
  f32 normal_y;
  if (((f64) quad[2].x) < bounds[0])
  {
    if (((f64) delta->x) <= 0.0)
      return 0;
    if (((f64) (quad[2].x + delta->x)) < bounds[0])
      return 0;
    cross = (bounds[0] - ((f64) quad[2].x)) * ((f64) delta->y);
    if ((((bounds[2] - ((f64) quad[2].y)) * ((f64) delta->x)) < cross) || (cross < ((bounds[3] - ((f64) quad[2].y)) * ((f64) delta->x))))
    {
      start.x = (f32) (bounds[0] - ((f64) delta->x));
      start.y = (f32) (bounds[2] - ((f64) delta->y));
      end.x = (f32) bounds[0];
      end.y = (f32) bounds[2];
      if (func_8010BC10_us(quad[3], quad[2], start, end, &hit) == 1)
      {
        normal_y = 0.707f;
        normal_x = -0.707f;
        goto diagonal_response;
      }
      start.y = (f32) (bounds[3] - ((f64) delta->y));
      end.y = (f32) bounds[3];
      if (func_8010BC10_us(quad[2], quad[1], start, end, &hit) != 1)
        return 0;
      normal_x = -0.707f;
      {
        delta->x = end.x - hit.x;
        delta->y = end.y - hit.y;
        contact->x = hit.x;
        contact->y = hit.y;
        normal->x = normal_x;
        normal->y = normal_x;
        return 1;
      }
    }
    delta->y = (f32) (cross / ((f64) delta->x));
    delta->x = (f32) (bounds[0] - ((f64) quad[2].x));
    contact->x = (f32) bounds[0];
    contact->y = quad[2].y + delta->y;
    normal->x = -1.0f;
    normal->y = 0.0f;
    return 2;
  }
  if (bounds[1] < ((f64) quad[0].x))
  {
    if (0.0 <= ((f64) delta->x))
      return 0;
    if (bounds[1] < ((f64) (quad[0].x + delta->x)))
      return 0;
    cross = (bounds[1] - ((f64) quad[0].x)) * ((f64) delta->y);
    if ((cross < ((bounds[2] - ((f64) quad[0].y)) * ((f64) delta->x))) || (((bounds[3] - ((f64) quad[0].y)) * ((f64) delta->x)) < cross))
    {
      start.x = (f32) (bounds[1] - ((f64) delta->x));
      start.y = (f32) (bounds[2] - ((f64) delta->y));
      end.x = (f32) bounds[1];
      end.y = (f32) bounds[2];
      if (func_8010BC10_us(quad[0], quad[3], start, end, &hit) == 1)
      {
        normal_x = 0.707f;
        {
          delta->x = end.x - hit.x;
          delta->y = end.y - hit.y;
          contact->x = hit.x;
          contact->y = hit.y;
          normal->x = normal_x;
          normal->y = normal_x;
          return 1;
        }
      }
      start.y = (f32) (bounds[3] - ((f64) delta->y));
      end.y = (f32) bounds[3];
      goto test_lower_right;
    }
    delta->y = (f32) (cross / ((f64) delta->x));
    delta->x = (f32) (bounds[1] - ((f64) quad[0].x));
    contact->x = (f32) bounds[1];
    contact->y = quad[0].y + delta->y;
    normal->x = 1.0f;
    normal->y = 0.0f;
    return 2;
  }
  if (bounds[2] < ((f64) quad[3].y))
  {
    if (0.0 <= ((f64) delta->y))
      return 0;
    if (bounds[2] < ((f64) (quad[3].y + delta->y)))
      return 0;
    cross = (bounds[2] - ((f64) quad[3].y)) * ((f64) delta->x);
    if ((((bounds[0] - ((f64) quad[3].x)) * ((f64) delta->y)) < cross) || (cross < ((bounds[1] - ((f64) quad[3].x)) * ((f64) delta->y))))
    {
      start.x = (f32) (bounds[0] - ((f64) delta->x));
      start.y = (f32) (bounds[2] - ((f64) delta->y));
      end.x = (f32) bounds[0];
      end.y = (f32) bounds[2];
      if (func_8010BC10_us(quad[3], quad[2], start, end, &hit) == 1)
      {
        normal_y = 0.707f;
        normal_x = -0.707f;
        goto diagonal_response;
      }
      start.x = (f32) (bounds[1] - ((f64) delta->x));
      end.x = (f32) bounds[1];
      if (func_8010BC10_us(quad[0], quad[3], start, end, &hit) != 1)
        return 0;
      normal_x = 0.707f;
      {
        delta->x = end.x - hit.x;
        delta->y = end.y - hit.y;
        contact->x = hit.x;
        contact->y = hit.y;
        normal->x = normal_x;
        normal->y = normal_x;
        return 1;
      }
    }
    delta->x = (f32) (cross / ((f64) delta->y));
    delta->y = (f32) (bounds[2] - ((f64) quad[3].y));
    contact->x = quad[3].x + delta->x;
    contact->y = (f32) bounds[2];
    normal->x = 0.0f;
    normal->y = 1.0f;
    return 2;
  }
  if (((f64) quad[1].y) < bounds[3])
  {
    if (((f64) delta->y) <= 0.0)
      return 0;
    if (((f64) (quad[1].y + delta->y)) < bounds[3])
      return 0;
    cross = (bounds[3] - ((f64) quad[1].y)) * ((f64) delta->x);
    if ((cross < ((bounds[0] - ((f64) quad[1].x)) * ((f64) delta->y))) || (((bounds[1] - ((f64) quad[1].x)) * ((f64) delta->y)) < cross))
    {
      start.x = (f32) (bounds[0] - ((f64) delta->x));
      start.y = (f32) (bounds[3] - ((f64) delta->y));
      end.x = (f32) bounds[0];
      end.y = (f32) bounds[3];
      if (func_8010BC10_us(quad[2], quad[1], start, end, &hit) == 1)
      {
        normal_x = -0.707f;
        {
          delta->x = end.x - hit.x;
          delta->y = end.y - hit.y;
          contact->x = hit.x;
          contact->y = hit.y;
          normal->x = normal_x;
          normal->y = normal_x;
          return 1;
        }
      }
      start.x = (f32) (bounds[1] - ((f64) delta->x));
      end.x = (f32) bounds[1];
      goto test_lower_right;
    }
    delta->x = (f32) (cross / ((f64) delta->y));
    delta->y = (f32) (bounds[3] - ((f64) quad[1].y));
    contact->x = quad[1].x + delta->x;
    contact->y = (f32) bounds[3];
    normal->x = 0.0f;
    normal->y = -1.0f;
    return 2;
  }
  end.x = (f32) bounds[0];
  end.y = (f32) bounds[3];
  start.x = end.x - delta->x;
  start.y = end.y - delta->y;
  if (func_8010BC10_us(quad[2], quad[1], start, end, &hit) == 1)
  {
    normal_x = -0.707f;
    {
      delta->x = end.x - hit.x;
      delta->y = end.y - hit.y;
      contact->x = hit.x;
      contact->y = hit.y;
      normal->x = normal_x;
      normal->y = normal_x;
      return 1;
    }
  }
  end.y = (f32) bounds[2];
  start.y = end.y - delta->y;
  if (func_8010BC10_us(quad[3], quad[2], start, end, &hit) == 1)
  {
    normal_y = 0.707f;
    normal_x = -0.707f;
    goto diagonal_response;
  }
  end.x = (f32) bounds[1];
  start.x = end.x - delta->x;
  if (func_8010BC10_us(quad[0], quad[3], start, end, &hit) == 1)
  {
    normal_x = 0.707f;
    {
      delta->x = end.x - hit.x;
      delta->y = end.y - hit.y;
      contact->x = hit.x;
      contact->y = hit.y;
      normal->x = normal_x;
      normal->y = normal_x;
      return 1;
    }
  }
  end.y = (f32) bounds[3];
  start.y = end.y - delta->y;
  test_lower_right:
  if (func_8010BC10_us(quad[1], quad[0], start, end, &hit) != 1)
    return 0;

  normal_y = -0.707f;
  normal_x = 0.707f;
  goto diagonal_response;
  delta->x = end.x - hit.x;
  delta->y = end.y - hit.y;
  contact->x = hit.x;
  contact->y = hit.y;
  normal->x = normal_x;
  normal->y = normal_x;
  return 1;
  diagonal_response:
  delta->x = end.x - hit.x;

  delta->y = end.y - hit.y;
  contact->x = hit.x;
  contact->y = hit.y;
  normal->x = normal_x;
  normal->y = normal_y;
  return 1;
}


